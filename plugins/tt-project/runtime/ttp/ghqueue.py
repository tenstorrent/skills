# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""gh through the user's workstation (delivery.gh_route "workstation"), for a lab host with no gh login
whose user's workstation takes no inbound connections. The rule: when gh only works on the user's
workstation, the workstation pulls and runs the queue; never the other way round.

- Here (the box): a gh call the harness cannot run (no login) goes in the host's gh outbox (OUTBOX under
  the tt-project home; each entry names its project and task) with its arguments, repo and a dedupe key,
  and exits HELD_RC. Its caller (a worker's gh, pr-watch, `ttp ci`) retries as for any held call; the
  same call again gets the workstation's answer once it is in: a read's while fresh (READ_FRESH_S), once;
  a write's for WRITE_KEEP_S, so a retried write never posts twice.
- There (the workstation): `ttp bridge gh-pull` logs in to the box (`ttp gh-queue take`), runs each call
  with its own gh as an argument list (never a shell string), only if allowed() passes there too, and
  puts the answers back (`ttp gh-queue put`). `"gh_pull": true` on a box in the bridge config keeps it
  running as a service.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, aitag
from .ghroute import HELD_RC, SETTING, WORKSTATION, _with_repo, is_write, local_inputs, mark, repo_of

OUTBOX = "gh-outbox"
READ_FRESH_S = 600            # a read's answer older than this is asked again
WRITE_KEEP_S = 86400          # a write's answer is kept this long: a retry gets it, never a second post
CLAIM_S = 900                 # a taken call with no answer by then is taken again
PENDING_S = {False: 3600, True: 7 * 86400}   # a call nobody took by then is dropped (read, write)
BATCH = 20
OUT_MAX = 4 << 20
GH_TIMEOUT_S = 120
REMOTE = 'PATH="$HOME/.local/bin:$PATH" ttp gh-queue'   # setup's default shim folder, off a non-login PATH
_KEY = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
_READS = {("pr", "view"), ("pr", "list"), ("pr", "checks"), ("issue", "view"), ("issue", "list")}
_POSTS = {("pr", "comment"), ("issue", "comment"), ("pr", "create")}
_API_FIELDS = {"-f", "-F", "--field", "--raw-field"}
_BUNDLED = re.compile(r"-[A-Za-z]\S+")   # -XPOST, -cw, -bText: a bundle or an attached value
_NEVER = {"--web", "-w", "--editor", "-e", "--delete-last"}   # opens a browser or editor there, or deletes
# The only flags each of these upkeep calls may carry (every flag takes a value; anything else is refused)
_EDIT_FLAGS = {"--title": "--title", "-t": "--title", "--body": "--body", "-b": "--body", "--body-file": "--body",
               "-F": "--body", "--repo": "--repo", "-R": "--repo", "--add-label": "--add-label"}
_DISPATCH_FLAGS = {"--ref": "--ref", "-r": "--ref", "--repo": "--repo", "-R": "--repo"}
_CLOSE_FLAGS = {"--comment": "--comment", "-c": "--comment", "--repo": "--repo", "-R": "--repo"}
_REPO_FLAGS = {"--repo": "--repo", "-R": "--repo"}
_RUN_ID = re.compile(r"[0-9]+")
_REPLY_PATH = re.compile(r"/?repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pulls/[0-9]+/comments/[0-9]+/replies")
_REPLY_FLAGS = {"-X": "method", "--method": "method", "-f": "field", "--raw-field": "field"}
# A project's own task branches (worktree's ttp/t<id>-<topic>, harness and revert branches); only these may be
# deleted, one plain ref each: no `..`, no %-encoding, no empty or dot-led segment, never main, master or default
OWN_PREFIX = "ttp/"
_DELETE_PATH = re.compile(r"/?repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/git/refs/heads/(.+)")
_REF_SEG = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")
_DELETE_FLAGS = {"-X": "method", "--method": "method"}
_PROTECTED = {"main", "master"}
_PREFIX = re.compile(r"[A-Za-z0-9_.-]+/")   # a granted branch prefix (delivery / bridge gh_branch_prefixes)
PUBLISH = "git-publish"    # the queue's own call: push one branch from a git bundle (`ttp push --publish`)
PUBLISH_MAX = 2 << 20      # a publish's bundle cap (bytes)
PUBLISHED = "published-branches.jsonl"   # each publish's pushed sha, kept past prune (`ttp push --retire` reads it)
GIT_URL = "https://github.com/{repo}.git"
_BUNDLE = re.compile(rb"# v[23] git bundle\n")
# `gh repo fork <owner>/<repo> --fork-name <name> --clone=false`: a fresh fork under the gh user, for a PR head
# whose old fork left the upstream fork network; never one that already exists (the workstation reads first)
_REPO = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def _body(argv: list[str], stdin: bytes) -> str | None:
    """The post's body: inline (--body/-b) or on stdin (--body-file -/-F -), else None."""
    body = None
    for i, a in enumerate(argv):
        name, eq, val = a.partition("=")
        if name in ("--body", "-b", "--body-file", "-F"):
            v = val if eq else (argv[i + 1] if i + 1 < len(argv) else None)
            body = (stdin.decode("utf-8", "replace") if v == "-" else None) if name in ("--body-file", "-F") else v
    return body


def _parse(rest: list[str], flags: dict) -> tuple[list[str], dict[str, list[str]], str]:
    """(positionals, {canonical flag: values}, why refused) of `rest` against an allowlist of flags that
    each take a value."""
    pos, opts, i = [], {}, 0
    while i < len(rest):
        a = rest[i]
        if not a.startswith("-") or a == "-":
            pos.append(a)
            i += 1
            continue
        name, eq, val = a.partition("=")
        if name not in flags:
            return pos, opts, f"`{name}` is not allowed here"
        if not eq:
            if i + 1 >= len(rest):
                return pos, opts, f"`{name}` has no value"
            val, i = rest[i + 1], i + 1
        opts.setdefault(flags[name], []).append(val)
        i += 1
    if any(len(v) > 1 for v in opts.values()):
        return pos, opts, "a flag is given twice"
    return pos, opts, ""


def _tagged(text: str | None) -> bool:
    return text is not None and aitag.ends_with_tag(text, aitag.DEFAULT)


def _upkeep(argv: list[str], stdin: bytes) -> str:
    """Why `gh pr edit` (title and body only) or `gh pr close` (an optional comment) is refused, or ""."""
    edit = argv[1] == "edit"
    pos, opts, why = _parse(argv[2:], _EDIT_FLAGS if edit else _CLOSE_FLAGS)
    if why:
        return f"`gh pr {argv[1]}`: {why}" + (" (only --title, --body or --body-file -)" if edit else
                                                " (only an optional --comment; never --delete-branch)")
    if len(pos) != 1:
        return f"`gh pr {argv[1]}` names exactly one PR"
    if edit:
        if not set(opts) & {"--title", "--body", "--add-label"}:
            return "`gh pr edit` runs only to change the title or body, or to add a label"
        body = opts.get("--body", [None])[0]
        is_file = any(a.split("=", 1)[0] in ("--body-file", "-F") for a in argv[2:])
        if is_file:
            body = stdin.decode("utf-8", "replace") if body == "-" else None
        if "--body" in opts and not _tagged(body):
            return f"the PR body (--body or --body-file -) does not end with the AI-authorship tag {aitag.DEFAULT!r}"
        return ""
    if "--comment" in opts and not _tagged(opts["--comment"][0]):
        return f"the close comment does not end with the AI-authorship tag {aitag.DEFAULT!r}"
    return ""


_INPUT_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
_INPUT_VAL = re.compile(r"[A-Za-z0-9._,:/@+=-]{0,128}")
INPUTS_MAX = 8


def _inputs(rest: list[str]) -> tuple[list[str], str]:
    """(`rest` without its `-f key=value` workflow inputs, why one is refused or ""): plain tokens only."""
    out, found, i = [], 0, 0
    while i < len(rest):
        if rest[i] != "-f":
            out.append(rest[i])
            i += 1
            continue
        key, eq, val = (rest[i + 1] if i + 1 < len(rest) else "").partition("=")
        found += 1
        if not eq or not _INPUT_KEY.fullmatch(key) or not _INPUT_VAL.fullmatch(val) or val.startswith("@"):
            return out, "an input is `-f key=value` with a plain key and a plain value (no spaces, quotes or @file)"
        i += 2
    return out, f"at most {INPUTS_MAX} inputs" if found > INPUTS_MAX else ""


def parse_dispatch(rest: list[str]) -> tuple[list[str], dict[str, list[str]], str]:
    """_parse() of `gh workflow run`'s words after `workflow run`, its plain `-f key=value` inputs taken out."""
    rest, why = _inputs(rest)
    return _parse(rest, _DISPATCH_FLAGS) if not why else ([], {}, why)


def _dispatch(argv: list[str]) -> tuple[str, str, str]:
    """(why `gh workflow run argv` is refused or "", the repo, the ref): one workflow name, --ref and
    --repo, and up to INPUTS_MAX plain `-f key=value` inputs; nothing else."""
    pos, opts, why = parse_dispatch(argv[2:])
    if why or len(pos) != 1 or "--ref" not in opts or "--repo" not in opts:
        return (f"`gh workflow run` takes one workflow, --ref, --repo and plain -f inputs ({why or 'form'})"), "", ""
    return "", opts["--repo"][0], opts["--ref"][0]


def _reply(argv: list[str]) -> str | None:
    """None unless `gh api` posts to a PR review comment's replies; then why it is refused, or "": one
    `-X POST` to exactly repos/<o>/<r>/pulls/<n>/comments/<id>/replies with one raw field, body, ending
    with the AI tag."""
    if not any(_REPLY_PATH.fullmatch(a) for a in argv[1:]):
        return None
    pos, opts, why = _parse(argv[1:], _REPLY_FLAGS)
    if why:
        return f"a review-comment reply: {why} (only -X POST and -f body=...)"
    if len(pos) != 1 or [m.upper() for m in opts.get("method", [])] != ["POST"]:
        return "a review-comment reply is one `-X POST` to one replies path"
    field = opts.get("field", [""])[0]
    if not field.startswith("body=") or not _tagged(field[5:]):
        return f"a review-comment reply carries one field, body, ending with the AI-authorship tag {aitag.DEFAULT!r}"
    return ""


def _gh_login(gh: str) -> str | None:
    """This workstation's gh user's login (`gh api user --jq .login`), or None when the read fails."""
    try:
        me = subprocess.run([gh, "api", "user", "--jq", ".login"], capture_output=True, timeout=GH_TIMEOUT_S)
        return None if me.returncode else me.stdout.decode().strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def _own_draft_reason(pr: object, login: str, act: str) -> str:
    """Why the PR record `pr` is not an open draft this workstation's gh user opened, or "": the author's
    login must match `login` without regard to case and isDraft must be True; `act` opens the refusal
    ("`gh pr close` closes only" / "`gh run rerun` reruns only a run of")."""
    if not isinstance(pr, dict):
        pr = {}
    author = pr.get("author") if isinstance(pr.get("author"), dict) else {}
    if not login or str(author.get("login") or "").lower() != login.lower():
        return f"{act} a draft this workstation's gh user opened, and this PR is someone else's"
    if pr.get("isDraft") is not True:
        return f"{act} a draft PR, and this one is not a draft"
    return ""


def own_draft(gh: str, argv: list[str]) -> str:
    """Why the workstation must not close the PR `gh pr close argv` names, or "": only a draft its own gh
    user opened, read just before the close."""
    pos, opts, why = _parse(argv[2:], _CLOSE_FLAGS)
    if why or len(pos) != 1:
        return why or "no single PR named"
    repo = [f"--repo={opts['--repo'][0]}"] if "--repo" in opts else []
    unread = "could not read the PR or this gh user to check it is our own draft"
    try:
        pr = subprocess.run([gh, "pr", "view", pos[0], *repo, "--json", "isDraft,author"], capture_output=True,
                            timeout=GH_TIMEOUT_S)
        login = _gh_login(gh)
        if pr.returncode or login is None:
            return unread
        v = json.loads(pr.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return unread
    return _own_draft_reason(v, login, "`gh pr close` closes only")


def _rerun(argv: list[str]) -> tuple[str, str, str]:
    """(why `gh run rerun argv` is refused or "", the run id, the repo or ""): only `run rerun <id>` with
    an optional --failed and --repo, nothing else (no --job, no --debug)."""
    rest = [a for a in argv[2:] if a != "--failed"]
    pos, opts, why = _parse(rest, _REPO_FLAGS)
    if why or len(argv) - 2 - len(rest) > 1:
        return f"`gh run rerun`: {why or 'a flag is given twice'} (only an optional --failed)", "", ""
    if len(pos) != 1 or not _RUN_ID.fullmatch(pos[0]):
        return "`gh run rerun` names exactly one run by its numeric id", "", ""
    return "", pos[0], opts.get("--repo", [""])[0]


def own_open_pr(gh: str, argv: list[str]) -> str:
    """Why the workstation must not run `gh workflow run argv` or `gh pr edit argv --add-label`, or "": the
    ref must be the head of an open PR its own gh user opened (a label: the named PR), never the default branch."""
    repo, ref, num = "", "", ""
    if argv[:2] == ["workflow", "run"]:
        why, repo, ref = _dispatch(argv)
    else:
        pos, opts, why = _parse(argv[2:], _EDIT_FLAGS)
        repo, num = opts.get("--repo", [""])[0], pos[0] if pos else ""
    if why or not repo:
        return why or "no repo named"
    unread = "could not read the PR or this gh user to check it is our own"
    try:
        view = [gh, "pr", "list", f"--repo={repo}", "--head", ref, "--state", "open", "--json", "author,headRefName"] if ref \
            else [gh, "pr", "view", num, f"--repo={repo}", "--json", "author,state,headRefName"]
        r = subprocess.run(view, capture_output=True, timeout=GH_TIMEOUT_S)
        found, login = json.loads(r.stdout) if r.returncode == 0 else None, _gh_login(gh)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return unread
    found = [found] if isinstance(found, dict) and found.get("state") == "OPEN" else found if isinstance(found, list) else []
    found = [p for p in found if isinstance(p, dict) and (not ref or p.get("headRefName") == ref)]
    if login is None or not found:
        return unread if login is None else "the ref is not the head of an open PR"
    if ref and ref == _default_branch(gh, repo):
        return f"`{ref}` is the repo's default branch"
    mine = [p for p in found if str((p.get("author") or {}).get("login") or "").lower() == login.lower()]
    return "" if mine else "that PR is someone else's"


def own_draft_run(gh: str, argv: list[str]) -> str:
    """Why the workstation must not rerun the run `gh run rerun argv` names, or "": read just before it,
    the run's head sha must be the head of an open draft PR this workstation's gh user opened, and its
    branch never the repo's default branch."""
    why, run, repo = _rerun(argv)
    if why or not repo:
        return why or "`gh run rerun` names no repo"
    unread = "could not resolve the run's head to check it is our own draft PR's"
    try:
        view = subprocess.run([gh, "run", "view", run, f"--repo={repo}", "--json", "headBranch,headSha"],
                              capture_output=True, timeout=GH_TIMEOUT_S)
        head = json.loads(view.stdout) if view.returncode == 0 else {}
        head = head if isinstance(head, dict) else {}
        branch, sha = str(head.get("headBranch") or ""), str(head.get("headSha") or "")
        if not branch or not _SHA.fullmatch(sha):
            return unread
        default = _default_branch(gh, repo)
        if not default:
            return "could not read the repo's default branch before a rerun"
        if branch == default:
            return f"the run is on `{branch}`, the repo's default branch, and is never rerun from here"
        prs = subprocess.run([gh, "pr", "list", f"--repo={repo}", "--head", branch, "--state", "open", "--json",
                              "isDraft,author,headRefOid"], capture_output=True, timeout=GH_TIMEOUT_S)
        if prs.returncode:
            return unread
        found = json.loads(prs.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return unread
    login = _gh_login(gh)
    if login is None:
        return unread
    found = [p for p in found if isinstance(p, dict) and p.get("headRefOid") == sha] if isinstance(found, list) else []
    if not found:
        return "the run is not on the head of an open PR"
    for p in found:
        if not (why := _own_draft_reason(p, login, "`gh run rerun` reruns only a run of")):
            return ""
    return why


def branch_problem(branch, prefixes=()) -> str:
    """Why the gh route must not publish or delete `branch`, or "": one plain branch under the project's own
    OWN_PREFIX or a granted prefix (`prefixes`), never main or master."""
    own = (OWN_PREFIX, *[x for x in prefixes or () if isinstance(x, str) and _PREFIX.fullmatch(x)])
    if not isinstance(branch, str) or branch in _PROTECTED or not branch.startswith(own):
        return f"only the project's own {' or '.join(own)} branches, never `{branch}`"
    if not all(_REF_SEG.fullmatch(seg) and not seg.endswith(".lock") for seg in branch.split("/")) or ".." in branch:
        return "one plain branch (no `..`, %-encoding or empty segment)"
    return ""


def _publish(argv: list[str], stdin: bytes, prefixes=()) -> str:
    """Why `git-publish <owner/repo> <branch> <sha>` (stdin: a git bundle holding <sha>) is refused, or ""."""
    if len(argv) != 4 or not _REPO.fullmatch(argv[1]) or set(argv[1].split("/")) & {".", ".."}:
        return f"`{PUBLISH}` names <owner>/<repo> <branch> <sha>"
    if why := branch_problem(argv[2], prefixes):
        return f"a publish names {why}"
    if not _SHA.fullmatch(argv[3]):
        return "a publish names its commit by the full sha"
    if not _BUNDLE.match(stdin or b"") or len(stdin) > PUBLISH_MAX:
        return f"a publish carries one git bundle of at most {PUBLISH_MAX >> 20} MiB"
    return ""


def _delete(argv: list[str], prefixes=()) -> str | None:
    """None unless `gh api` names a branch ref under repos/<o>/<r>/git/refs/heads/; then why it is refused,
    or "": exactly one `-X DELETE` of one of the project's own branches (OWN_PREFIX), nothing else."""
    if not any(_DELETE_PATH.fullmatch(a) for a in argv[1:]):
        return None
    pos, opts, why = _parse(argv[1:], _DELETE_FLAGS)
    if why:
        return f"a branch delete: {why} (only -X DELETE and the ref's path)"
    if len(pos) != 1 or [m.upper() for m in opts.get("method", [])] != ["DELETE"]:
        return "a branch ref is only deleted, with one `-X DELETE` of one path (other writes to refs are refused)"
    m = _DELETE_PATH.fullmatch(pos[0])
    branch = m.group(3) if m else ""
    if not m or {m.group(1), m.group(2)} & {".", ".."}:
        return "a branch delete names one repo as repos/<owner>/<repo>"
    return f"a branch delete names {why}" if (why := branch_problem(branch, prefixes)) else ""


def delete_target(argv: list[str]) -> tuple[str, str]:
    """(owner/repo, branch) of an allowed branch delete."""
    m = _DELETE_PATH.fullmatch(next(a for a in argv[1:] if _DELETE_PATH.fullmatch(a)))
    return f"{m.group(1)}/{m.group(2)}", m.group(3)


def _default_branch(gh: str, repo: str) -> str:
    try:
        r = subprocess.run([gh, "api", f"repos/{repo}", "--jq", ".default_branch"], capture_output=True,
                           timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.decode().strip() if r.returncode == 0 else ""


def own_branch(gh: str, argv: list[str]) -> tuple[str, str]:
    """(why the workstation must not delete the branch `gh api -X DELETE` names, or "", its tip sha): read
    just before the delete, so the answer records the tip and the branch can be restored; never the repo's
    default branch."""
    repo, branch = delete_target(argv)
    default = _default_branch(gh, repo)
    if not default:
        return "could not read the repo's default branch before deleting a branch", ""
    if branch == default:
        return f"`{branch}` is the repo's default branch and is never deleted", ""
    try:
        ref = subprocess.run([gh, "api", f"repos/{repo}/git/ref/heads/{branch}", "--jq", ".object.sha"],
                             capture_output=True, timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        ref = None
    tip = ref.stdout.decode().strip() if ref else ""
    if not ref or ref.returncode or not _SHA.fullmatch(tip):
        return f"could not read `{branch}`'s tip before deleting it (is it there?)", ""
    return "", tip


def _fork(argv: list[str]) -> tuple[str, str, str]:
    """(why `gh repo fork argv` is refused or "", the upstream owner/repo, the fork's name): only
    `repo fork <owner>/<repo> --fork-name <name> --clone=false`, nothing else."""
    pos, name, clone, rest = [], [], [], argv[2:]
    for i, a in enumerate(rest):
        if a == "--clone=false":
            clone.append(a)
        elif a.startswith("--fork-name="):
            name.append(a.split("=", 1)[1])
        elif a == "--fork-name":
            name.append(rest[i + 1] if i + 1 < len(rest) else "")
        elif not (i and rest[i - 1] == "--fork-name"):
            pos.append(a)
    if any(a.startswith("-") for a in pos) or len(pos) != 1 or len(name) != 1 or clone != ["--clone=false"]:
        return ("`gh repo fork` runs only as `repo fork <owner>/<repo> --fork-name <name> --clone=false`, "
                "no other flags"), "", ""
    m = _REPO.fullmatch(pos[0])
    if not m or {m.group(1), m.group(2)} & {".", ".."} or not _NAME.fullmatch(name[0]) or name[0] in (".", ".."):
        return "`gh repo fork` names one repo as <owner>/<repo> and a plain --fork-name", "", ""
    return "", pos[0], name[0]


def fork_free(gh: str, argv: list[str]) -> str:
    """Why the workstation must not run the fork `gh repo fork argv` asks for, or "": read just before it,
    <this gh user>/<name> must not exist yet (gh would hand back an old fork instead of a fresh one)."""
    name = _fork(argv)[2]
    login = _gh_login(gh)
    if login is None or not _NAME.fullmatch(login):
        return "could not read this gh user to check the fork's name is free"
    try:
        there = subprocess.run([gh, "api", f"repos/{login}/{name}", "--jq", ".full_name"], capture_output=True,
                               timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return "could not check the fork's name is free"
    if there.returncode == 0:
        return f"`{login}/{name}` already exists; pick another --fork-name"
    if "404" not in there.stderr.decode("utf-8", "replace") + there.stdout.decode("utf-8", "replace"):
        return f"could not check that `{login}/{name}` is free"
    return ""


def allowed(argv, stdin: bytes = b"", prefixes=()) -> str:
    """Why the workstation must not run `gh argv`, or "": the allowlist, checked on both sides. `gh api`
    GET reads, pr view/list/checks, issue view/list, pr and issue comments, `pr create --draft`, a reply
    to a PR review comment (`gh api -X POST .../pulls/<n>/comments/<id>/replies -f body=...`), deleting one
    of the project's own ttp/ branches (`gh api -X DELETE repos/<o>/<r>/git/refs/heads/ttp/...`; the
    workstation also reads its tip first and refuses the default branch), `pr edit`
    of the title or body only and `pr close` (the workstation also checks the PR is its own draft), each
    post, body or comment ending with the AI-authorship tag, and `repo fork <owner>/<repo> --fork-name
    <name> --clone=false` (the workstation also checks <its gh user>/<name> does not exist yet), `workflow run
    <name> --ref <branch> --repo <r>` with up to 8 plain `-f key=value` inputs and `pr edit --add-label` (the workstation also checks the PR is its own open one), `run rerun
    <id>` with an optional --failed (the workstation also checks the run is on its own open draft PR's
    head, never the default branch), and the
    queue's own `git-publish` of one such branch (a new branch or a fast-forward); `prefixes` grants branch
    prefixes beyond ttp/ (the workstation's bridge config, the box's delivery.gh_branch_prefixes); never
    `pr ready`, a merge, a reviewer, `run cancel` or anything else."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\0" not in a for a in argv):
        return "not a list of plain arguments"
    if bad := next((a for a in argv if _BUNDLED.fullmatch(a)), None):
        return f"`{bad}`: short options go one per word, values apart (so none hides a method, field or --web)"
    if argv[0] == PUBLISH:
        return _publish(argv, stdin, prefixes)
    if argv[0] == "api":
        if (why := _reply(argv)) is not None:
            return why
        if (why := _delete(argv, prefixes)) is not None:
            return why
        get = any(a.upper() == "--METHOD=GET" or (a in ("-X", "--method") and nxt.upper() == "GET")
                  for a, nxt in zip(argv, [*argv[1:], ""]))
        names = [a.split("=", 1)[0] for a in argv if a.startswith("-")]
        if (is_write(argv) or any(a.lstrip("/").startswith("graphql") for a in argv[1:] if not a.startswith("-"))
                or "--input" in names or (not get and set(names) & _API_FIELDS)
                or any(a.startswith("--method=") and a.upper() != "--METHOD=GET" for a in argv)
                or any("override" in a.lower() for a in argv)):
            return "`gh api` runs GET reads only"
        return ""
    if bad := next((a for a in argv if a.split("=", 1)[0] in _NEVER), None):
        return f"`{bad}` is not run there"
    pair = tuple(argv[:2])
    if pair in _READS:
        return ""
    if pair in (("pr", "edit"), ("pr", "close")):
        return _upkeep(argv, stdin)
    if pair == ("repo", "fork"):
        return _fork(argv)[0]
    if pair == ("run", "rerun"):
        return _rerun(argv)[0]
    if pair == ("workflow", "run"):
        return _dispatch(argv)[0]
    if pair not in _POSTS:
        return f"`gh {' '.join(argv[:2])}` {NOT_LISTED}"
    if pair == ("pr", "create") and not any(a in ("--draft", "-d") for a in argv):
        return "`gh pr create` runs only with --draft"
    body = _body(argv, stdin)
    if body is None or not aitag.ends_with_tag(body, aitag.DEFAULT):
        return f"the post's body (--body or --body-file -) does not end with the AI-authorship tag {aitag.DEFAULT!r}"
    return ""


def outbox(home=None) -> Path:
    from . import ghauth
    return ghauth._home(home) / OUTBOX


def _load(f: Path) -> dict | None:
    try:
        v = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _write(f: Path, v: dict) -> None:
    from .project import write_json
    write_json(f, v, 0o600)


def prune(d: Path, now: float | None = None) -> None:
    """Drop stale answers and calls nobody took."""
    now = now or time.time()
    for f in d.glob("*.*"):
        if f.suffix not in (".q", ".done"):
            continue
        v = _load(f) or {}
        age = now - float(v.get("at") or v.get("queued_at") or 0)
        limit = (WRITE_KEEP_S if v.get("write") else READ_FRESH_S) if f.suffix == ".done" else PENDING_S[bool(v.get("write"))]
        if age > limit:
            f.unlink(missing_ok=True)


def _project(cwd: str | None) -> str:
    """The project's registered name (`ttp list`), never this host's paths: every project's folder is
    tt-project, so its name tells the workstation's log which project asked. Else its configured name, its
    root folder's name, then the folder's."""
    env = os.environ.get("TTP_PROJECT")
    try:
        from .project import Project, here, load_registry
        p = Project(env) if env else here(cwd)
        if p is None:
            return ""
        for name, e in (load_registry().get("projects") or {}).items():
            if isinstance(e, dict) and e.get("dir") and Path(str(e["dir"])).resolve() == p.root:
                return str(name)
        return p.name
    except Exception:
        return Path(env).name if env else ""


def enqueue(args: list[str], input: bytes | None = None, cwd: str | None = None, home=None, prefixes=(),
            key: str = "") -> int:
    """`gh args` here with no login: the workstation's answer if it is in (its output on ours), else the
    call queued once (HELD_RC). `key`: this attempt's own key (a retry of a write whose answer failed)."""
    sent, data, why = local_inputs(args, input, cwd)
    if why:
        print(f"gh (tt-project): held: {why}; it was not queued for the workstation (exit {HELD_RC}).", file=sys.stderr)
        return HELD_RC
    repo = repo_of(cwd)
    sent, data = _with_repo(sent, repo), data or b""
    if why := allowed(sent, data, prefixes):
        print(f"gh (tt-project): refused: {why} ({SETTING} is {WORKSTATION}).", file=sys.stderr)
        return 1
    return _queued(sent, data, repo, cwd, home, f"gh {' '.join(args[:2])}", key)


def _key(repo: str, sent: list[str], data: bytes) -> str:
    return hashlib.sha256(json.dumps([repo, sent, base64.b64encode(data).decode()]).encode()).hexdigest()[:32]


NOT_LISTED = "is not on the workstation's allowlist"
NO_INPUTS = "takes one workflow, --ref and --repo, no inputs"   # an older release's refusal of `-f` inputs


def _older_bridge(sent: list[str], data: bytes, got: dict) -> str:
    """The stored answer's stderr, with an old bridge's refusal of a call this host's own allowlist has (the
    generic not-on-the-allowlist one, or the old no-inputs one) made plain: the workstation runs an older
    release, and how to update it."""
    err = str(got.get("stderr") or "")
    if not (err.rstrip().endswith(NOT_LISTED) or NO_INPUTS in err) or allowed(sent, data):
        return err
    ver = got.get("bridge_version")
    said = ver if isinstance(ver, str) and ver else "it reports no version"
    return (f"refused on the workstation: its bridge runs an older release ({said}) that does not take "
            f"`gh {' '.join(sent[:2])}` like this; update tt-project on the workstation (rerun its install line, "
            f"docs/install.md, Update), then run `ttp bridge setup <box>` there to restart its gh-pull service on the "
            f"new release, then retry (this host runs {__version__})\n")


def _queued(sent: list[str], data: bytes, repo: str, cwd: str | None, home, what: str, key: str = "") -> int:
    """The answer to an allowed call if it is in (its output on ours), else the call queued once (HELD_RC)."""
    key = key or _key(repo, sent, data)
    d = outbox(home)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    prune(d)
    done = d / f"{key}.done"
    if (got := _load(done)) is not None:
        sys.stdout.write(str(got.get("stdout") or ""))
        sys.stderr.write(_older_bridge(sent, data, got))
        if not got.get("write"):
            done.unlink(missing_ok=True)   # a read is answered once; the next one asks again
        mark(True, via=WORKSTATION)
        return int(got.get("rc", 1)) if str(got.get("rc", 1)).lstrip("-").isdigit() else 1
    if not (d / f"{key}.q").exists() and not (d / f"{key}.taken").exists():
        _write(d / f"{key}.q", {"key": key, "argv": sent, "repo": repo, "project": _project(cwd),
                                "task": os.environ.get("TTP_TASK", ""), "write": sent[0] == PUBLISH or is_write(sent),
                                "stdin": base64.b64encode(data).decode(), "queued_at": time.time()})
    print(f"gh (tt-project): `{what}` is queued for the user's workstation ({SETTING} is "
          f"{WORKSTATION}); the same call again returns its answer once in (exit {HELD_RC}).", file=sys.stderr)
    return HELD_RC


def take(home=None, now: float | None = None) -> list[dict]:
    """Claim up to BATCH queued calls (and any taken more than CLAIM_S ago with no answer)."""
    now = now or time.time()
    d = outbox(home)
    if not d.is_dir():
        return []
    prune(d, now)
    out = []
    for f in sorted([*d.glob("*.q"), *d.glob("*.taken")], key=lambda f: f.stat().st_mtime if f.exists() else 0):
        if len(out) >= BATCH or not _KEY.fullmatch(f.stem):
            continue
        if f.suffix == ".taken" and now - f.stat().st_mtime < CLAIM_S:
            continue
        if f.suffix == ".taken" and (_load(f) or {}).get("write"):   # it may have posted: never sent twice
            _write(d / f"{f.stem}.done", {"rc": 1, "write": True, "stdout": "", "at": now, "stderr":
                   "the workstation took this gh write but sent no answer; it was not sent again (it may have posted)"})
            f.unlink(missing_ok=True)
            continue
        claim = d / f"{f.stem}.taken"
        try:
            os.rename(f, claim)
            os.utime(claim)
        except OSError:
            continue   # another puller took it
        if (v := _load(claim)) is not None:
            out.append({k: v.get(k) for k in ("key", "argv", "repo", "project", "task", "stdin")})
    return out


DELETED = "deleted-branches.jsonl"   # each branch delete's tip, kept past prune, so a branch can be restored


def _durable(f: Path, row: dict) -> None:
    from .project import durable_append
    durable_append(f, json.dumps(row) + "\n")


def _record_delete(d, repo, branch, tip, rc, project, task, at) -> None:
    """Keep one branch delete's tip in `d`'s DELETED record, so the branch can be restored."""
    Path(d).mkdir(parents=True, exist_ok=True, mode=0o700)
    _durable(Path(d) / DELETED, {"repo": repo, "branch": branch, "tip": tip, "rc": rc, "project": project,
                                 "task": task, "at": at})


def put(results, home=None) -> int:
    """Store the workstation's answers to calls taken here; returns how many were stored."""
    d, n = outbox(home), 0
    for r in results if isinstance(results, list) else []:
        key = r.get("key") if isinstance(r, dict) else None
        if not isinstance(key, str) or not _KEY.fullmatch(key) or (v := _load(d / f"{key}.taken")) is None:
            continue
        rc = r.get("rc")
        done = {"rc": rc if isinstance(rc, int) else 1, "write": bool(v.get("write")),
                "stdout": str(r.get("stdout") or "")[:OUT_MAX], "stderr": str(r.get("stderr") or "")[:OUT_MAX],
                "at": time.time(), "bridge_version": str(r.get("version") or "")[:40]}
        tip = r.get("tip")
        argv = v.get("argv") if isinstance(v.get("argv"), list) and v.get("argv") else [""]
        if argv[0] == PUBLISH and len(argv) == 4 and done["rc"] == 0 and r.get("pushed") == argv[3]:
            done.update(pushed=argv[3], branch=argv[2])
            _durable(d / PUBLISHED, {"repo": argv[1], "branch": argv[2], "sha": argv[3],
                                     "project": v.get("project"), "task": v.get("task"), "at": done["at"]})
        if isinstance(tip, str) and _SHA.fullmatch(tip) and argv[0] == "api" \
                and any(_DELETE_PATH.fullmatch(a) for a in argv[1:]):
            repo, branch = delete_target(v["argv"])
            done.update(tip=tip, branch=branch)
            _record_delete(d, repo, branch, tip, done["rc"], v.get("project"), v.get("task"), done["at"])
        _write(d / f"{key}.done", done)
        (d / f"{key}.taken").unlink(missing_ok=True)
        n += 1
    return n


# --- the workstation --------------------------------------------------------------------------------

def publish_here(gh: str, argv: list[str], bundle: bytes) -> tuple[int, str]:
    """Push a `git-publish`'s commit to its branch with this computer's own login (gh as git's helper for
    github.com): in a scratch repo, fetch the bundle's prerequisites (commits the remote already has),
    unbundle, then `git push <url> <sha>:refs/heads/<branch>`, with no `+`, so only a new branch or a
    fast-forward, never a delete; never the repo's default branch. (rc, what happened)."""
    repo, branch, sha = argv[1:4]
    default = _default_branch(gh, repo)
    if not default or branch == default:
        return 1, ("refused on the workstation: " + (f"`{branch}` is the repo's default branch" if default else
                   "could not read the repo's default branch before publishing"))
    url = GIT_URL.format(repo=repo)
    head = bundle.split(b"\n\n", 1)[0].decode("utf-8", "replace").splitlines()
    prereq = [x[1:].split()[0] for x in head if x.startswith("-") and len(x) > 1]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    helper = "credential.https://github.com.helper"
    with tempfile.TemporaryDirectory(prefix="ttp-publish-") as t:
        (Path(t) / "b.bundle").write_bytes(bundle)
        git = ["git", "-C", f"{t}/r", "-c", f"{helper}=", "-c", f"{helper}=!{gh} auth git-" + "credential"]
        for step in (["git", "init", "-q", "--bare", f"{t}/r"],
                     *([[*git, "fetch", "-q", "--depth=1", url, *prereq]] if prereq else []),
                     [*git, "bundle", "unbundle", f"{t}/b.bundle"], [*git, "cat-file", "-e", f"{sha}^{{commit}}"],
                     [*git, "push", "-q", url, f"{sha}:refs/heads/{branch}"]):
            name = next(x for x in step[1:] if x in ("init", "fetch", "bundle", "cat-file", "push"))
            try:
                p = subprocess.run(step, capture_output=True, timeout=4 * GH_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired) as e:
                return 1, f"git {name} could not run on the workstation: {e}"
            if p.returncode:
                return p.returncode, f"git {name} failed on the workstation: {p.stderr.decode('utf-8', 'replace')[-600:]}"
    return 0, f"pushed {sha} to {repo} branch {branch}"


POLICY_TIMEOUT_S = 30   # a gh policy command's time per entry
POLICY_FIELDS = ("key", "argv", "repo", "project", "task", "stdin")   # its stdin JSON (stdin: base64), a stable contract


def policy(cmd: str, entry: dict) -> str:
    """The workstation's own gh policy (a box's "gh_policy_cmd" in the bridge config), run once per allowed entry
    as an argument list with the entry's POLICY_FIELDS as JSON on stdin: exit 0 allows (""); exit 1 refuses, its
    first output line the reason; any other exit, a timeout or a command that cannot run refuses as an error."""
    if not cmd:
        return ""
    from . import boundedchild
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return f"error: the gh policy command could not run: {e}"
    p = boundedchild.run(argv, POLICY_TIMEOUT_S, input=json.dumps({k: entry.get(k) for k in POLICY_FIELDS}))
    if p.timed_out:   # its whole process group is gone: nothing it started acts later
        return f"error: the gh policy command timed out after {POLICY_TIMEOUT_S}s"
    if p.rc is None:
        return f"error: the gh policy command {p.error}"
    if p.rc == 0:
        return ""
    first = next((ln.strip() for ln in f"{p.out}\n{p.err}".splitlines() if ln.strip()), "")[:300]
    if p.rc == 1:
        return f"the gh policy command refused it: {first or 'no reason given'}"
    return f"error: the gh policy command failed (exit {p.rc}){': ' + first if first else ''}"


def answer(entry: dict, gh: str | None = None, prefixes=(), where: str = "on the workstation",
           policy_cmd: str = "") -> dict | None:
    """Run one taken call with this computer's gh, if allowed() here (`prefixes`: the branch prefixes this
    computer's bridge config grants beyond ttp/; `where`: how its refusals and faults say where they happened;
    `policy_cmd`: the workstation's own policy(), which may refuse more); its answer."""
    key = entry.get("key")
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        return None
    try:
        stdin = base64.b64decode(entry.get("stdin") or "", validate=True)
    except (ValueError, TypeError):
        return {"key": key, "rc": 1, "stdout": "", "stderr": f"refused {where}: stdin is not base64"}
    argv = entry.get("argv")
    if (why := allowed(argv, stdin, prefixes)) or (why := policy(policy_cmd, entry)):
        return {"key": key, "rc": 1, "stdout": "", "stderr": f"refused {where}: {why}"}
    gh = gh or shutil.which("gh", path=os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin") or "gh"
    if argv[0] == PUBLISH:
        rc, said = publish_here(gh, argv, stdin)
        return {"key": key, "rc": rc, "stdout": f"{said}\n" if rc == 0 else "", "stderr": "" if rc == 0 else said,
                **({"pushed": argv[3]} if rc == 0 else {})}
    if argv[:2] == ["pr", "close"] and (why := own_draft(gh, argv)):
        return {"key": key, "rc": 1, "stdout": "", "stderr": f"refused {where}: {why}"}
    if argv[:2] == ["run", "rerun"] and (why := own_draft_run(gh, argv)):
        return {"key": key, "rc": 1, "stdout": "", "stderr": f"refused {where}: {why}"}
    if (argv[:2] == ["workflow", "run"] or (argv[:2] == ["pr", "edit"] and any(
            a.split("=", 1)[0] == "--add-label" for a in argv))) and (why := own_open_pr(gh, argv)):
        return {"key": key, "rc": 1, "stdout": "", "stderr": f"refused {where}: {why}"}
    if argv[:2] == ["repo", "fork"] and (why := fork_free(gh, argv)):
        return {"key": key, "rc": 1, "stdout": "", "stderr": f"refused {where}: {why}"}
    tip = ""
    if argv[0] == "api" and _delete(argv, prefixes) == "":
        why, tip = own_branch(gh, argv)
        if why:
            return {"key": key, "rc": 1, "stdout": "", "stderr": f"refused {where}: {why}"}
    try:
        p = subprocess.run([gh, *argv], input=stdin, capture_output=True, timeout=GH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        out = {"key": key, "rc": 124, "stdout": "", "stderr": f"gh timed out after {GH_TIMEOUT_S}s {where}"}
    except OSError as e:
        return {"key": key, "rc": 127, "stdout": "", "stderr": f"gh could not run {where}: {e}"}
    else:
        out = {"key": key, "rc": p.returncode, "stdout": p.stdout.decode("utf-8", "replace"),
               "stderr": p.stderr.decode("utf-8", "replace")}
    if tip:   # the tip read just before the delete, so the branch can be restored
        out["tip"] = tip
        out["stderr"] += f"\nbranch tip before the delete: {tip}\n"
    return out


def _answer_or_error(entry: dict, gh: str | None, prefixes, policy_cmd: str) -> dict | None:
    """answer(), or an error answer for an entry whose answering raised: one bad entry never stops the pull loop."""
    key = entry.get("key")
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        return None
    try:
        return answer(entry, gh, prefixes, policy_cmd=policy_cmd)
    except Exception as e:   # noqa: BLE001 - any fault answers this entry and the loop goes on
        return {"key": key, "rc": 1, "stdout": "", "stderr": f"error on the workstation: {type(e).__name__}: {e}"[:600]}


def pull(box: str, gh: str | None = None, prefixes=(), policy_cmd: str = "") -> str:
    """One round on `box`: take its queue, answer each call here, put the answers back."""
    from .bridge import _box
    r = _box(box, f"{REMOTE} take", timeout=60)
    if r.returncode != 0:
        return f"{box}: could not take the gh queue: {(r.stderr.strip() or f'exit {r.returncode}')[:200]}"
    try:
        entries = json.loads(r.stdout or "[]")
    except ValueError:
        return f"{box}: the gh queue's answer is not JSON"
    results = [{**a, "version": __version__} for e in (entries if isinstance(entries, list) else [])
               if isinstance(e, dict) and (a := _answer_or_error(e, gh, prefixes, policy_cmd))]
    if not results:
        return f"{box}: no gh calls queued"
    p = _box(box, f"{REMOTE} put", timeout=60, inp=json.dumps(results))
    if p.returncode != 0:
        return f"{box}: ran {len(results)} gh call(s) but could not put the answers back: {p.stderr.strip()[:200]}"
    refused = sum(1 for a in results if str(a.get("stderr", "")).startswith("refused on the workstation"))
    failed = sum(1 for a in results if str(a.get("stderr", "")).startswith("error on the workstation"))
    return (f"{box}: answered {len(results)} gh call(s)" + (f", {refused} refused" if refused else "")
            + (f", {failed} failed here" if failed else ""))


def service_argv(box: str) -> list[str]:
    """The workstation service that keeps pulling `box`'s queue."""
    from .release import service_runtime
    return ["/usr/bin/env", f"PYTHONPATH={service_runtime()}", sys.executable, "-m", "ttp", "bridge", "gh-pull", box,
            "--loop"]


def queue_argv(args: list[str]) -> list[str]:
    """The command a caller runs for `gh args` in workstation mode (ghroute.routed)."""
    return ["/usr/bin/env", f"PYTHONPATH={Path(__file__).resolve().parent.parent}", sys.executable, "-m", "ttp",
            "gh-queue", "run", "--", *args]


# --- the box: publish and retire one branch through the workstation ----------------------------------

def _git(cwd, *a, binary: bool = False) -> subprocess.CompletedProcess:
    from . import gitmemo
    return gitmemo.run(["git", *a], cwd=cwd or None, capture_output=True, text=not binary, timeout=120,
                       stdin=subprocess.DEVNULL)


def _branch_call(cwd: str | None, branch: str, local: bool = False) -> tuple[str, str, list, object]:
    """(why the gh route cannot take this branch call here, or "", owner/repo, granted prefixes, project).
    `local`: the call may also run with this host's own logged-in gh while the route is unset."""
    from . import ghroute
    from .project import here
    if ghroute._value(cwd=cwd) != WORKSTATION and not local:
        return f"publishing and retiring a branch go through {SETTING} \"{WORKSTATION}\"", "", [], None
    if not (repo := repo_of(cwd)):
        return "this checkout's origin is not a GitHub repo", "", [], None
    try:
        p = here(cwd)
    except Exception:
        p = None
    grants = ((p.config() if p else {}).get("delivery") or {}).get("gh_branch_prefixes") or []
    grants = grants if isinstance(grants, list) else []
    return branch_problem(branch, grants), repo, grants, p


def publish(branch: str, cwd: str | None = None, home=None) -> int:
    """`ttp push --publish <branch>`: queue the branch's commits that origin's refs here do not hold, as a git
    bundle, for the workstation to push with its own login (a new branch or a fast-forward). The answer,
    once in, names the pushed sha (rerun the same call); HELD_RC until then."""
    from . import prsize
    why, repo, grants, p = _branch_call(cwd, branch)
    if not why and (budget := prsize.budget_gate(p, None, branch)):
        print(f"ttp push --publish: refused: {budget}.", file=sys.stderr)
        return 1
    sha = _git(cwd, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}").stdout.strip() if not why else ""
    if not why and not _SHA.fullmatch(sha):
        why = f"no local branch `{branch}` here"
    if why:
        print(f"ttp push --publish: refused: {why}.", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="ttp-publish-") as t:
        b = _git(cwd, "bundle", "create", "-q", f"{t}/b.bundle", f"refs/heads/{branch}", "--not", "--remotes=origin",
                 binary=True)
        if b.returncode:
            print(f"ttp push --publish: refused: no bundle of `{branch}`'s new commits over origin's refs here "
                  f"({b.stderr.decode('utf-8', 'replace').strip()[-300:]}).", file=sys.stderr)
            return 1
        data = (Path(t) / "b.bundle").read_bytes()
    sent = [PUBLISH, repo, branch, sha]
    if why := allowed(sent, data, grants):
        print(f"ttp push --publish: refused: {why}.", file=sys.stderr)
        return 1
    return _queued(sent, data, repo, cwd, home, f"git push {branch} ({sha[:12]})")


def retire(branch: str, cwd: str | None = None, home=None) -> int:
    """`ttp push --retire <branch>`: delete one of the project's own published branches through the
    workstation, or with this host's own gh while delivery.gh_route is unset and that gh is logged in.
    Refused unless this project pushed it (a publish here, or its push records), its prefix is allowed, no open PR uses it (a gh read, held until answered) and no open task or worktree here uses it.
    The tip is saved first, as refs/ttp-private/retired/<branch> and a bundle under state/retired/; the
    workstation reads the tip again and refuses the default branch. The delete this host runs itself
    goes straight to the real gh binary (ghhold.real_on), never the run's gh wrapper: no file or variable
    a run can write or set authorizes it."""
    from . import ghroute
    here_gh = ghroute._value(cwd=cwd) == "" and ghroute.local_ok()   # unset route, this host's gh logged in
    why, repo, grants, p = _branch_call(cwd, branch, here_gh)
    delete = ["api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}"]

    def ask(argv: list[str], key: str = "") -> int:
        """The gh call through the workstation queue, or with this host's own gh (the same checks)."""
        if not here_gh:
            return enqueue(argv, None, cwd, home, grants, key)
        argv = _with_repo(argv, repo)
        from . import ghhold, untrusted
        if why := untrusted.gh_refusal(argv):   # the wrapper's own check, which the real gh skips
            print(f"ttp push --retire: {why}", file=sys.stderr)
            return 1
        ghhold.restore(os.environ)   # the run's held gh login, put back as the wrapper does
        got = answer({"key": _key(repo, argv, b""), "argv": argv, "stdin": ""},
                     gh=ghhold.real_on(os.environ.get("PATH", "")),
                     prefixes=grants, where="here") or {}
        sys.stdout.write(str(got.get("stdout") or ""))
        sys.stderr.write(str(got.get("stderr") or ""))
        rc = got.get("rc") if isinstance(got.get("rc"), int) else 1
        if got.get("tip"):
            _record_delete(outbox(home), repo, branch, got["tip"], rc, _project(cwd), os.environ.get("TTP_TASK", ""),
                           time.time())
        return rc

    # Only a delete that succeeded answers a new request. A failed one (refused or not run on the
    # workstation) keeps its receipt as it is; the next attempt takes a key chained from it. This host's own
    # gh answers at once and keeps no receipt.
    key, failed = _key(repo, delete, b""), None
    while not why and not here_gh and (got := _load(outbox(home) / f"{key}.done")) is not None:
        if got.get("rc") == 0:
            return _queued(delete, b"", repo, cwd, home, f"gh api -X DELETE {branch}", key)   # already done
        key, failed = _key(repo, [*delete, key], b""), got
    if failed:
        print(f"ttp push --retire: the last delete of `{branch}` failed (exit {failed.get('rc')}): "
              f"{str(failed.get('stderr') or '').strip()[-300:]}; this request runs it again.", file=sys.stderr)
    if why:
        print(f"ttp push --retire: refused: {why}.", file=sys.stderr)
        return 1
    from .benchrun import json_lines
    published = [r for r in json_lines(outbox(home) / PUBLISHED) if isinstance(r, dict) and r.get("repo") == repo
                 and r.get("branch") == branch and r.get("project") == _project(cwd)]
    from . import push
    pushed = p is not None and any(
        (v := _load(f)) and str(v.get("target") or "").endswith("/" + branch)
        # a push batch's receipt, or this project's own single push (`ttp push --own`)
        and (v.get("kind") == "batch" or v.get("own") is True) and push.pushed(f)
        for f in (p.state / "pushes").glob("*.json"))
    # No publish record: the local branch, else origin's remote-tracking ref, else the head its newest
    # task handed off on a shared branch.
    from .integrate import MEMBER_HEAD_REF
    refs = [f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"] + ([] if p is None else [
        f"{MEMBER_HEAD_REF}{r['id']}" for r in p.db.q("SELECT id FROM tasks WHERE branch=? ORDER BY id DESC LIMIT 1",
                                                      (branch,))])
    tip = (published[-1].get("sha") if published else "") or next(
        (sha for r in refs if (sha := _git(cwd, "rev-parse", "--verify", "-q", f"{r}^{{commit}}").stdout.strip())), "")
    worktrees = _git(cwd, "worktree", "list", "--porcelain").stdout.splitlines()
    tasks = [] if p is None else [r["id"] for r in p.db.q(
        "SELECT id FROM tasks WHERE branch=? AND status NOT IN ('done','cancelled','failed') AND id!=?",
        (branch, os.environ.get("TTP_TASK") or -1))]
    if not (published or pushed):
        why = f"no publish or push record shows this project pushed `{branch}`"
    elif f"branch refs/heads/{branch}" in worktrees:
        why = f"a worktree here has `{branch}` checked out"
    elif tasks:
        why = f"open task(s) {', '.join(f'#{t}' for t in tasks)} use `{branch}`"
    elif not _SHA.fullmatch(tip or "") or _git(cwd, "cat-file", "-e", f"{tip}^{{commit}}").returncode:
        why = f"`{branch}`'s tip is not here to save first"
    if why:
        print(f"ttp push --retire: refused: {why}.", file=sys.stderr)
        return 1
    import contextlib
    import io
    got = io.StringIO()
    with contextlib.redirect_stdout(got):
        rc = ask(["pr", "list", "--head", branch, "--state", "open", "--json", "number"])
    if rc and here_gh:
        print(f"ttp push --retire: the open-PR check on `{branch}` failed (exit {rc}).", file=sys.stderr)
        return rc
    if rc:
        print(f"ttp push --retire: the open-PR check on `{branch}` is not answered yet (exit {rc}); "
              "run the same call again once it is.", file=sys.stderr)
        return rc
    try:
        prs = json.loads(got.getvalue() or "[]")
    except ValueError:
        prs = None
    if prs != []:
        print(f"ttp push --retire: refused: an open PR uses `{branch}` (or the check's answer is unreadable).",
              file=sys.stderr)
        return 1
    ref = f"refs/ttp-private/retired/{branch}"
    keep = (p.state if p is not None else outbox(home)) / "retired"
    keep.mkdir(parents=True, exist_ok=True)
    saved = keep / (branch.replace("/", "%2F") + ".bundle")
    if _git(cwd, "update-ref", ref, tip).returncode or _git(cwd, "bundle", "create", "-q", str(saved), ref).returncode:
        print(f"ttp push --retire: refused: could not save `{branch}`'s tip {tip} first.", file=sys.stderr)
        return 1
    print(f"ttp push --retire: saved {tip} as {ref} and {saved}.", file=sys.stderr)
    return ask(delete, key)
