"""Where a pull request's head branch lives, for `ttp push` and the claimed-push checks.

A task's pr_branch may be the head of an open PR whose head repository is a fork, not the repository
the push would otherwise go to (origin, or the push branch's remote). `ttp push` asks gh for the open
PR of that branch (lookup) and, when its head is such a fork, pushes through a git remote for it: one
this repo already has for that repository on the same host, else one it adds as ttp-head-<owner>
(fork_remote). origin is never rewritten. The fork's main, master and default branch are refused.
The daemon's push verification reads the same remote when a claim on the pr_branch is missing on
origin (claim_remote), so a commit that is the PR's head is not taken for a lost push; the push
queue reads it before taking a review-approved pr_branch the push remote lacks for absent. Those
daemon-tick callers never read the fork's default branch (only a push refusal needs it) and pass their
pushclaim.Budget: gh then asks only while it has time left, its timeout cut to what is left, and
pushclaim.OutOfTime passes through once it is spent.

No open PR, a same-repository PR, or gh unable to answer: the push goes where it went before, with
the reason said. Answers are kept for the process (and in the run's folder, for the run).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

GH_TIMEOUT_S = 20
FIELDS = "headRepository,headRepositoryOwner,isCrossRepository,headRefName,state,url"
PREFIX = "ttp-head-"       # remotes this module adds for a fork
CACHE = "pr_head.json"     # answers kept in the run's folder
MEMO_TTL_S = 600           # how long a long-lived process (the daemon) reuses an answer
PROTECTED = {"HEAD", "main", "master"}

_memo: dict = {}
# <prefix><owner>/<name>[.git][/]: the last two path parts of a URL, scp-like address or path.
_SLUG = re.compile(r"^(?P<prefix>.*[/:])(?P<owner>[^/:]+)/(?P<name>[^/:]+?)(?:\.git)?/?$")
_HOST = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://)?(?:[^@/]+@)?(?P<host>[^/:]+)[:/]")


def _git(repo, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"git {args[0]} timed out")


def _local(url: str) -> bool:
    return url.startswith(("/", "./", "../", "~", "file://")) or not _HOST.match(url)


def slug(url: str) -> tuple[str, str] | None:
    """(owner, name) a repository URL ends in, lower-cased, or None."""
    m = _SLUG.match(url.strip())
    return (m["owner"].lower(), m["name"].lower()) if m else None


def host(url: str) -> str:
    """The host of a network URL ("" for a local path)."""
    return "" if _local(url) else _HOST.match(url)["host"].lower()


def head_url(base_url: str, owner: str, name: str) -> str:
    """`base_url` (the repository the PR is on) with its owner/name swapped for the head repository's."""
    m = _SLUG.match(base_url.strip())
    if not m:
        return ""
    return f"{m['prefix']}{owner}/{name}{'.git' if base_url.strip().rstrip('/').endswith('.git') else ''}"


def _url(repo, remote: str) -> str:
    return _git(repo, "remote", "get-url", "--push", remote).stdout.strip() if remote else ""


def _gh_repo(url: str) -> str:
    """gh's --repo for a network URL ([host/]owner/name), "" for a local path (gh then reads the repo's remotes)."""
    s = slug(url)
    return f"{host(url)}/{s[0]}/{s[1]}" if s and not _local(url) else ""


def _cache_file() -> Path | None:
    d = os.environ.get("TTP_RUN_DIR")
    return Path(d) / CACHE if d and Path(d).is_dir() else None


def lookup(repo, branch: str, base_url: str = "", budget=None) -> tuple[dict | None, str]:
    """(the open PR whose head branch is `branch`, why none): {"owner", "name", "cross", "url"} as
    gh answers for the repository at `base_url` (else the one gh finds from the repo's remotes). gh
    asks within `budget` (a pushclaim.Budget) when given; a kept answer asks nothing."""
    key = (str(Path(repo).resolve()), branch, base_url)
    hit = _memo.get(key)
    if hit and time.time() - hit[0] < MEMO_TTL_S:
        return hit[1]
    f = _cache_file()
    try:
        kept = json.loads(f.read_text()) if f and f.is_file() else {}
    except (OSError, ValueError):
        kept = {}
    ck = f"{branch}\t{base_url}"
    if isinstance(kept, dict) and isinstance(kept.get(ck), list) and len(kept[ck]) == 2:
        out = (kept[ck][0], kept[ck][1])
    else:
        out = _ask(repo, branch, base_url, budget)
        if f and (out[0] or out[1].startswith(("no open PR", "the PR"))):   # answers, not gh failures
            try:
                from .project import durable_write
                durable_write(f, json.dumps({**(kept if isinstance(kept, dict) else {}), ck: list(out)}))
            except OSError:
                pass
    _memo[key] = (time.time(), out)
    return out


def _ask(repo, branch: str, base_url: str, budget=None) -> tuple[dict | None, str]:
    args = ["gh", "pr", "view", branch, "--json", FIELDS]
    if r := _gh_repo(base_url):
        args += ["--repo", r]

    def gh(t: float) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(args, cwd=str(repo), capture_output=True, text=True, timeout=t,
                                  stdin=subprocess.DEVNULL, env={**os.environ, "GH_PROMPT_DISABLED": "1"})
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", "TimeoutExpired")
    try:
        r = (budget.run("gh pr view", GH_TIMEOUT_S, gh, lambda r: r.returncode == 124 and r.stderr == "TimeoutExpired")
             if budget is not None else gh(GH_TIMEOUT_S))
    except FileNotFoundError:
        return None, "gh is not installed, so the PR for it could not be looked up"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"gh could not answer ({type(e).__name__})"
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1][:200]
        if "no pull requests found" in err.lower() or "no open pull requests" in err.lower():
            return None, f"no open PR has {branch} as its head"
        return None, f"gh could not answer ({err})"
    try:
        d = json.loads(r.stdout)
        state, ref = str(d.get("state") or ""), str(d.get("headRefName") or "")
        owner = str((d.get("headRepositoryOwner") or {}).get("login") or "")
        name = str((d.get("headRepository") or {}).get("name") or "")
    except (ValueError, AttributeError):
        return None, "gh could not answer (unreadable JSON)"
    if state.upper() != "OPEN":
        return None, f"the PR for {branch} is {state.lower() or 'not open'}"
    if ref != branch or not owner or not name:
        return None, f"no open PR has {branch} as its head"
    return {"owner": owner, "name": name, "cross": bool(d.get("isCrossRepository")),
            "url": str(d.get("url") or "")}, ""


def default_branch(repo, remote: str) -> str:
    """The default branch of `remote` (its HEAD), "" when it cannot be read."""
    r = _git(repo, "ls-remote", "--symref", remote, "HEAD", timeout=60)
    m = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", r.stdout, re.M) if r.returncode == 0 else None
    return m.group(1) if m else ""


def _ensure(repo, url: str, owner: str) -> str:
    """A remote of `repo` for the repository at `url`: one it has whose URL names the same repository on
    the same host, else one added as ttp-head-<owner> (a free name). origin is never changed."""
    want, at = slug(url), host(url)
    for r in _git(repo, "remote").stdout.split():
        urls = {_git(repo, "remote", "get-url", r).stdout.strip(), _url(repo, r)} - {""}
        if any(slug(u) == want and host(u) == at for u in urls):
            return r
    base = PREFIX + (re.sub(r"[^A-Za-z0-9._-]", "-", owner).strip(".-") or "fork")
    name, n = base, 1
    while name in _git(repo, "remote").stdout.split():
        n += 1
        name = f"{base}-{n}"
    add = _git(repo, "remote", "add", name, url)
    if add.returncode != 0:
        raise ValueError(f"could not add a remote for the PR's head repository ({add.stderr.strip()[-200:]}); "
                         f"not pushing")
    return name


def fork_remote(repo, branch: str, remote: str, budget=None, refusal: bool = True) -> tuple[str, str]:
    """(remote, note) for a push of `branch` that would go to `remote`: the remote of the open PR's
    head repository when that is a fork, else ("", why the push stays on `remote`, or "").
    Raises ValueError for the fork's main, master or default branch (the default branch read only
    with `refusal`): never pushed this way. gh asks within `budget` (see lookup)."""
    base = _url(repo, "origin") or _url(repo, remote)
    info, why = lookup(repo, branch, base, budget)
    if not info:
        return "", why
    target = _url(repo, remote)
    if not info["cross"] or slug(target) == (info["owner"].lower(), info["name"].lower()):
        return "", ""
    url = head_url(base, info["owner"], info["name"])
    if not url:
        return "", f"the PR's head repository is {info['owner']}/{info['name']}, but no URL for it follows from {base}"
    if branch in PROTECTED:
        raise ValueError(f"the PR's head branch on {info['owner']}/{info['name']} is {branch}: never pushed this way")
    fork = _ensure(repo, url, info["owner"])
    if not refusal:
        return fork, f"the open PR {info['url'] or 'for ' + branch} has its head on {info['owner']}/{info['name']}"
    default = default_branch(repo, fork)
    if not default:
        raise ValueError(f"could not read the default branch of {fork} ({url}), the PR's head repository; "
                         f"not pushing")
    if branch == default:
        raise ValueError(f"{branch} is the default branch of {fork} ({url}), the PR's head repository: "
                         f"never pushed this way")
    return fork, (f"the open PR {info['url'] or 'for ' + branch} has its head on {info['owner']}/{info['name']}, "
                  f"not {remote}: pushing to remote {fork}")


def claim_remote(repo, branch: str, budget=None, remote: str = "origin") -> tuple[str, str]:
    """For the daemon's push checks: (the remote of `branch`'s open PR head when that is a fork, why
    else or how). No default-branch read; gh asks within `budget` (see lookup). Raises only
    pushclaim.OutOfTime, when `budget` is spent: the caller asks again on a later tick."""
    try:
        return fork_remote(repo, branch, remote, budget, refusal=False)
    except (ValueError, OSError) as e:
        return "", str(e)
