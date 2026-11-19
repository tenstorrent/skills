# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Anonymous, cached reads of public GitHub repositories, for when this host's gh is logged out (ghauth).

pr-watch (watchers.watch_prs) reads a PR here when gh cannot: GitHub's REST API with no token (urllib
sends no Authorization header and reads no netrc) and the PR's head with `git ls-remote` (credential
helpers off, no prompt). It never reads or prints a credential store.

Anonymous calls are limited per IP (60 an hour), shared with every process on the host, so:
- each answer is cached host-wide under the tt-project home (DIR, one file per URL): a PR 25 min, a
  sha-pinned read (check runs once all are done; comments and reviews per PR update time) 30 days, a 404 6 h;
- the PR's head is resolved to a sha with `git ls-remote` (no API call), so its check runs are read by sha;
- at most `cap` calls (`watchers.gh_anon_cap`, default 30) go out per rolling hour, and none while GitHub's
  own X-RateLimit-Remaining is FLOOR or less before its reset. Over either, an answer up to STALE_S old is
  served; with none, Spent is raised.
A repository it cannot read (private, gone) raises Unreadable. TTP_GH_ANON_API (tests) sets the API base.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DIR = "gh-anon"
CAP = 30             # anonymous calls per rolling hour from this host (`watchers.gh_anon_cap`)
FLOOR = 5            # GitHub's last few anonymous calls of its hour are never spent
STALE_S = 6 * 3600   # what a spent budget may still serve
TTL_OPEN = 25 * 60
TTL_PINNED = 30 * 86400
TTL_404 = 6 * 3600
TIMEOUT_S = 20.0


class Spent(Exception):
    """The anonymous call budget is spent and no cached answer is left."""


class Unreadable(Exception):
    """The repository or PR cannot be read without a login (private, gone)."""


def _api() -> str:
    return (os.environ.get("TTP_GH_ANON_API") or "https://api.github.com").rstrip("/")


def _dir(home: Path | None) -> Path:
    from . import ghauth
    d = ghauth._home(home) / DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(d: Path, key: str) -> Path:
    return d / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".json")


def _cached(d: Path, key: str, ttl: float) -> dict | None:
    try:
        e = json.loads(_path(d, key).read_text())
    except (OSError, ValueError):
        return None
    return e if isinstance(e, dict) and time.time() - float(e.get("t") or 0) <= ttl else None


def _put(d: Path, key: str, status: int, body) -> None:
    from .project import durable_write
    # a cache: a power cut costs one more call at most, so no sync
    durable_write(_path(d, key), json.dumps({"key": key, "t": time.time(), "status": status, "body": body}),
                  sync=False)


def _budget(d: Path, fn):
    from .project import durable_write
    lk = os.open(d / "budget.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            b = json.loads((d / "budget.json").read_text())
        except (OSError, ValueError):
            b = {}
        b = b if isinstance(b, dict) else {}
        got = fn(b)
        durable_write(d / "budget.json", json.dumps(b))
        return got
    finally:
        os.close(lk)


def _take(d: Path, cap: int) -> bool:
    """Reserve one call under the host cap and GitHub's own remaining count; False when spent."""
    def take(b: dict) -> bool:
        now = time.time()
        b["calls"] = [t for t in b.get("calls") or [] if now - float(t) < 3600]
        rem, reset = b.get("remaining"), float(b.get("reset") or 0)
        if len(b["calls"]) >= cap or (rem is not None and int(rem) <= FLOOR and now < reset):
            return False
        b["calls"].append(now)
        return True
    return _budget(d, take)


def calls_last_hour(home: Path | None = None) -> int:
    return _budget(_dir(home), lambda b: len([t for t in b.get("calls") or [] if time.time() - float(t) < 3600]))


def _note_limit(d: Path, headers) -> None:
    try:
        rem, reset = int(headers.get("X-RateLimit-Remaining")), int(headers.get("X-RateLimit-Reset"))
    except (TypeError, ValueError):
        return
    _budget(d, lambda b: b.update(remaining=rem, reset=reset))


def get(path: str, ttl: float, home: Path | None = None, cap: int = CAP, key: str | None = None):
    """The JSON body of a GET of `path` (under the API base), from the cache while fresh enough. `key`
    names the cache entry (default: the URL), so a caller can pin it to a sha or an update time."""
    d, url = _dir(home), _api() + "/" + path.lstrip("/")
    key = key or url
    e = _cached(d, key, ttl)
    if e is not None and e["status"] == 404 and time.time() - float(e["t"]) > TTL_404:
        e = None
    if e is None and not _take(d, cap):
        e = _cached(d, key, STALE_S)
        if e is None:
            raise Spent(f"anonymous GitHub budget spent ({cap} calls an hour from this host) and no cached answer")
    if e is None:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "tt-project",
                                                   "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                _note_limit(d, r.headers)
                status, body = r.status, json.loads(r.read().decode("utf-8", "replace") or "null")
        except urllib.error.HTTPError as h:
            _note_limit(d, h.headers)
            if h.code in (403, 429) and h.headers.get("X-RateLimit-Remaining") == "0":
                if (e := _cached(d, key, STALE_S)) is None:
                    raise Spent(f"GitHub's anonymous rate limit is spent (HTTP {h.code})") from None
                return e["body"]
            if h.code in (401, 403, 404):
                _put(d, key, 404, None)
                raise Unreadable(f"HTTP {h.code}") from None
            raise OSError(f"HTTP {h.code}") from None
        except ValueError as x:
            raise OSError(f"bad JSON: {x}") from None
        _put(d, key, status, body)
        return body
    if e["status"] == 404:
        raise Unreadable("HTTP 404 (cached)")
    return e["body"]


def head_sha(repo: str, ref: str, timeout: float = TIMEOUT_S) -> str | None:
    """The sha `ref` names in the public `repo` (owner/name), by `git ls-remote`: no API call, no
    credential helper, no prompt, and an empty HOME with no global or system git config, so no netrc,
    extra header or URL rewrite adds a login either. None when it cannot say."""
    base = os.environ.get("TTP_GH_ANON_GIT") or "https://github.com"
    try:
        with tempfile.TemporaryDirectory(prefix="ttp-gh-anon-") as empty:
            env = {**os.environ, "HOME": empty, "XDG_CONFIG_HOME": empty, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true",
                   "SSH_ASKPASS": "true"}
            r = subprocess.run(["git", "-c", "credential.helper=", "ls-remote", f"{base}/{repo}", ref],
                               capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        sha, _, name = line.partition("\t")
        if name == ref and len(sha) == 40:
            return sha
    return None


def _who(u: dict | None) -> dict:
    u = u or {}
    return {"__typename": "Bot" if u.get("type") == "Bot" else "User", "login": u.get("login") or ""}


def _decision(reviews: list[dict]) -> str:
    last: dict = {}
    for r in reviews:
        if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            last[r["author"]["login"]] = r["state"]
    states = set(last.values())
    return "CHANGES_REQUESTED" if "CHANGES_REQUESTED" in states else "APPROVED" if "APPROVED" in states else ""


def pr_view(repo: str, number: int, home: Path | None = None, cap: int = CAP) -> dict:
    """The PR as `gh pr view --json` prints watchers.PR_FIELDS, read anonymously. Comments and reviews
    are read again only once the PR's update time moves; check runs and commit statuses only once its head
    moves. A call over the budget raises Spent: the PR is then unread, never read as passing."""
    p = get(f"repos/{repo}/pulls/{number}", TTL_OPEN, home, cap)
    stamp = f"{repo}#{number}@{p.get('updated_at')}"
    comments = get(f"repos/{repo}/issues/{number}/comments?per_page=100", TTL_PINNED, home, cap, key="c " + stamp)
    reviews = [{"id": r.get("node_id") or r.get("id"), "author": _who(r.get("user")), "state": r.get("state") or "",
                "body": r.get("body") or "", "submittedAt": r.get("submitted_at") or ""}
               for r in get(f"repos/{repo}/pulls/{number}/reviews?per_page=100", TTL_PINNED, home, cap,
                            key="r " + stamp)]
    sha = head_sha(repo, f"refs/pull/{number}/head") or (p.get("head") or {}).get("sha") or ""
    checks = []
    if sha:
        k = f"k {repo}@{sha}"
        done = _cached(_dir(home), k + " done", TTL_PINNED)
        runs = done["body"] if done else get(f"repos/{repo}/commits/{sha}/check-runs?per_page=100", TTL_OPEN,
                                               home, cap, key=k)
        checks = [{"__typename": "CheckRun", "name": c.get("name") or "?", "status": str(c.get("status") or "").upper(),
                   "conclusion": str(c.get("conclusion") or "").upper()} for c in (runs or {}).get("check_runs") or []]
        if checks and all(c["status"] == "COMPLETED" for c in checks):
            _put(_dir(home), k + " done", 200, runs)   # a finished head's runs no longer change
        # Legacy commit statuses (the combined status) count as gh's statusCheckRollup counts them.
        s = f"s {repo}@{sha}"
        done = _cached(_dir(home), s + " done", TTL_PINNED)
        combined = done["body"] if done else get(f"repos/{repo}/commits/{sha}/status?per_page=100", TTL_OPEN,
                                                   home, cap, key=s)
        statuses = [{"__typename": "StatusContext", "context": c.get("context") or "?",
                     "state": str(c.get("state") or "").upper()} for c in (combined or {}).get("statuses") or []]
        if statuses and all(c["state"] != "PENDING" for c in statuses):
            _put(_dir(home), s + " done", 200, combined)   # every status final: pinned by sha
        checks += statuses
    m = p.get("mergeable")
    return {"state": "MERGED" if p.get("merged_at") else "CLOSED" if p.get("state") == "closed" else "OPEN",
            "isDraft": bool(p.get("draft")), "reviewDecision": _decision(reviews),
            "mergeable": "UNKNOWN" if m is None else "MERGEABLE" if m else "CONFLICTING",
            "statusCheckRollup": checks, "reviews": reviews, "url": p.get("html_url") or "",
            "comments": [{"author": _who(c.get("user")), "body": c.get("body") or "",
                          "createdAt": c.get("created_at") or "", "url": c.get("html_url") or ""} for c in comments or []],
            "title": p.get("title") or "", "author": _who(p.get("user")), "headRefOid": sha,
            "headRefName": (p.get("head") or {}).get("ref") or ""}
