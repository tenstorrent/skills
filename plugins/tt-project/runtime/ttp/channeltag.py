# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Moving a release-channel tag forward while delivery.never_force is on.

Git needs a force to update an existing tag, even one that only moves forward, so never_force
(gitguard.never_force_refusal) would leave a release channel stuck. delivery.channel_tags (a list
of tag names; empty by default) names the tags a project moves forward. `ttp push --move-tag <tag>
<commit>` (move) runs the one push form the gitguard lets through under never_force:

    git push --no-follow-tags --force-with-lease=refs/tags/<tag>:<old> <remote> <new>:refs/tags/<tag>

and only when every rule holds (refusal): the tag is listed; the push updates that one ref on the
push remote (delivery.push_branch's); the lease is the tag's current value where the remote pushes to,
read fresh; the new commit descends from the old tagged one (forward only); and the new commit is on
the remote push branch. Anything else forced stays refused: force pushes to a branch, tag deletes,
backward or sideways moves, unlisted tags, a missing or stale lease, several refs at once. Each
allowed move is logged to the project's state/channel_tags.jsonl (tag, old, new, time).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

LOG = "channel_tags.jsonl"   # under the project's state: one line per allowed or made move
GIT_TIMEOUT_S = 120
# the only `git push` options the move form may carry besides its lease
QUIET_OPTS = {"-q", "--quiet", "-v", "--verbose", "--porcelain", "--progress", "--no-progress"}
SHA_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def tags_of(v: Any) -> list[str]:
    """delivery.channel_tags as tag names (a list, a JSON list or one per line); empty, the default,
    moves no tag. A `refs/tags/` prefix is dropped. ValueError for a name git would not take."""
    from .push import _entries
    out = []
    for t in _entries(v):
        name = str(t).strip().removeprefix("refs/tags/") if not isinstance(t, dict) else ""
        if not name or subprocess.run(["git", "check-ref-format", f"refs/tags/{name}"],
                                      capture_output=True, timeout=30).returncode:
            raise ValueError(f"delivery.channel_tags: {t!r} is not a tag name")
        out.append(name)
    return list(dict.fromkeys(out))


def channel_tags(cfg: dict) -> list[str]:
    """The project's delivery.channel_tags; a broken value reads as none."""
    try:
        return tags_of((cfg.get("delivery") or {}).get("channel_tags"))
    except (ValueError, OSError, subprocess.SubprocessError):
        return []


def problems(delivery: dict) -> list[str]:
    """delivery.channel_tags problems, one line each."""
    try:
        tags_of(delivery.get("channel_tags"))
    except ValueError as e:
        return [f"{e}; no tag moves under delivery.never_force"]
    return []


def plain_globals(globals_: list[str]) -> bool:
    """Whether git's global options before `push` are only `-C <dir>`: a `-c` (a pushInsteadOf, a
    remote's URL) could send the push somewhere the checks never read."""
    return len(globals_) % 2 == 0 and all(globals_[i] == "-C" for i in range(0, len(globals_), 2))


def parse(rest: list[str]) -> dict | None:
    """The move form's parts of `git push <rest>`: {remote, src, tag, lease}, or None when `rest` is
    anything else (another option, no or several leases, a `+`, a delete, several refspecs, a source
    that is not a full sha: a name such as HEAD resolves when the hook checks it, in the hook's cwd,
    not when and where the command later pushes it)."""
    lease, follow, positional, words = None, False, False, []
    for a in rest:
        if positional or not a.startswith("-"):
            words.append(a)
        elif a == "--":
            positional = True
        elif a.startswith("--force-with-lease="):
            if lease is not None:
                return None
            lease = a.split("=", 1)[1]
        elif a == "--no-follow-tags":
            follow = True
        elif a not in QUIET_OPTS:
            return None
    if lease is None or not follow or len(words) != 2:
        return None
    remote, spec = words
    src, sep, dst = spec.partition(":")
    ref, sep2, old = lease.partition(":")
    if not (sep and SHA_RE.fullmatch(src) and dst.startswith("refs/tags/") and sep2 and ref == dst):
        return None
    return {"remote": remote, "src": src, "tag": dst.removeprefix("refs/tags/"), "lease": old}


def _git(prefix: list[str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*prefix, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)


def remote_value(prefix: list[str], remote: str, tag: str) -> tuple[str, str] | None:
    """(the tag's value on the remote, the commit it peels to), ("", "") when it has none, None when
    the remote cannot be read."""
    out = _git(prefix, "ls-remote", "--tags", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    if out.returncode:
        return None
    rows = dict(reversed(ln.split("\t", 1)) for ln in out.stdout.splitlines() if "\t" in ln)
    raw = rows.get(f"refs/tags/{tag}", "")
    return raw, rows.get(f"refs/tags/{tag}^{{}}", raw)


def push_url(prefix: list[str], remote: str) -> str | None:
    """Where `git push <remote>` goes, for reading the tag and the push branch there: the remote's
    name when it pushes where it fetches from, else its push URL (a pushurl or pushInsteadOf); None
    with several push URLs (several refs updated) or no such remote."""
    urls = _git(prefix, "remote", "get-url", "--push", "--all", remote)
    fetch = _git(prefix, "remote", "get-url", remote)
    found = urls.stdout.split() if urls.returncode == 0 else []
    if len(found) != 1 or fetch.returncode:
        return None
    return remote if found == [fetch.stdout.strip()] else found[0]


def _commit(prefix: list[str], rev: str) -> str:
    out = _git(prefix, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else ""


def push_remote(prefix: list[str], cfg: dict) -> tuple[str, str]:
    """(remote, branch) of delivery.push_branch, as `ttp push` reads it; ("", "") when unset."""
    from .push import _split_target
    ref = str((cfg.get("delivery") or {}).get("push_branch") or "").strip()
    if not ref:
        return "", ""
    return _split_target(ref, _git(prefix, "remote").stdout.split())


def refusal(prefix: list[str], move: dict, cfg: dict) -> tuple[str, dict]:
    """(why the move must not run, "" when it may; its facts: tag, remote, old, new). `prefix` runs
    git in the repository (`git -C <repo>` or the push's own global options)."""
    tag, facts = move["tag"], {"tag": move["tag"], "remote": move["remote"]}
    if tag not in channel_tags(cfg):
        return f"tag {tag} is not in delivery.channel_tags", facts
    remote, branch = push_remote(prefix, cfg)
    if not remote:
        return "no delivery.push_branch: a channel tag moves only to a commit on it", facts
    if move["remote"] != remote:
        return f"a channel tag moves only on the push remote {remote}, not {move['remote']}", facts
    side = push_url(prefix, remote)
    if side is None:
        return f"{remote} has several push URLs or none: a channel tag moves on one remote only", facts
    now = remote_value(prefix, side, tag)
    if now is None:
        return f"could not read refs/tags/{tag} on {remote}", facts
    raw, old = now
    if not raw:
        return f"{remote} has no tag {tag}: create it with a plain push (no force needed)", facts
    if not SHA_RE.fullmatch(move["lease"]):
        return f"the lease must be the tag's full current value on {remote}: refs/tags/{tag}:{raw}", facts
    if move["lease"] != raw:
        return f"stale lease: refs/tags/{tag} on {remote} is {raw}, not {move['lease']}", facts
    new = _commit(prefix, move["src"])
    if not new:
        return f"{move['src']} is not a commit here", facts
    facts.update(old=raw, new=new)
    if not _commit(prefix, old) and _git(prefix, "fetch", "-q", "--no-tags", side, f"refs/tags/{tag}").returncode:
        return f"could not fetch the commit {tag} tags now ({old[:12]})", facts
    if _git(prefix, "merge-base", "--is-ancestor", old, new).returncode:
        return f"{new[:12]} does not descend from {old[:12]}, where {tag} is now: a channel tag moves only forward", facts
    from .push import tracking_ref
    track = tracking_ref(side, branch)
    if _git(prefix, "fetch", "-q", "--no-tags", side, f"+refs/heads/{branch}:{track}").returncode:
        return f"could not fetch {remote}/{branch}", facts
    if _git(prefix, "merge-base", "--is-ancestor", new, track).returncode:
        return f"{new[:12]} is not on {remote}/{branch}: a channel tag moves only to a commit on the push branch", facts
    return "", facts


def log(p, facts: dict, event: str) -> None:
    """Append one move (allowed by the gitguard, or made by `ttp push --move-tag`) to the log."""
    import os
    row = {"ts": time.time(), "event": event, "task": os.environ.get("TTP_TASK", ""),
           **{k: facts.get(k, "") for k in ("tag", "remote", "old", "new")}}
    from .project import durable_append
    durable_append(p.state / LOG, json.dumps(row) + "\n")


def argv(remote: str, tag: str, old: str, new: str) -> list[str]:
    """The `git push` arguments of the move form."""
    return ["push", "--no-follow-tags", f"--force-with-lease=refs/tags/{tag}:{old}", remote, f"{new}:refs/tags/{tag}"]


def move(p, repo, tag: str, commit: str, say) -> int:
    """`ttp push --move-tag <tag> <commit>`: move the channel tag forward to `commit` under a lease on
    its current remote value. 0 moved (or already there), 2 refused, 1 the push failed (a lease that
    went stale meanwhile, the remote refused, a git call that timed out)."""
    try:
        return _move(p, repo, tag, commit, say)
    except (OSError, subprocess.SubprocessError) as e:
        say(f"ttp push: moving {tag} failed: {type(e).__name__}: {e}")
        return 1


def _move(p, repo, tag: str, commit: str, say) -> int:
    cfg = p.config()
    prefix = ["git", "-C", str(repo)]
    tag = tag.removeprefix("refs/tags/")
    remote, _ = push_remote(prefix, cfg)
    side = push_url(prefix, remote) if remote and tag in channel_tags(cfg) else None
    now = remote_value(prefix, side, tag) if side else None
    lease = now[0] if now else ""
    new = _commit(prefix, commit)
    if now and new and now[1] == new:
        say(f"ttp push: {tag} is already at {new[:12]} on {remote}")
        return 0
    why, facts = refusal(prefix, {"remote": remote, "src": new or commit, "tag": tag, "lease": lease}, cfg)
    if why:
        say(f"ttp push: not moving {tag}: {why}")
        return 2
    # `git` from PATH: inside a run that is the harness's wrapper, whose gitguard checks this push again
    out = subprocess.run([*prefix, *argv(remote, tag, facts["old"], facts["new"])], text=True,
                         capture_output=True, timeout=GIT_TIMEOUT_S)
    if out.returncode:
        say(f"ttp push: moving {tag} failed (git exit {out.returncode}): {(out.stderr or out.stdout).strip()[-600:]}")
        return 1
    log(p, facts, "moved")
    say(f"ttp push: moved {tag} on {remote} from {facts['old'][:12]} to {facts['new'][:12]}")
    return 0
