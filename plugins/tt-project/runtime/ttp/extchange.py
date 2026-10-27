"""A code task's change published in another repository (`published` in its hand-off).

A code task that works in a clone of another repository has no diff in the project's own git: its
hand-off names the branch and head it published there, `{"repo": "<owner/name or remote URL>",
"branch": "<branch>", "head": "<sha>"}`, and the daemon queues a review-only review of exactly that
head (daemon._auto_review). An invalid entry is ignored with its reason, never raised. No git, no
network: the reviewer fetches it."""
from __future__ import annotations

import re

FIELD = "published"
HEAD_LABEL = "published_head:"   # on a review of an external change: the head it was queued for
NO_PUSH_LINE = "no_push: change lives in an external repo"

_HEAD = re.compile(r"[0-9a-f]{7,64}")
_OWNER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+")
_URL = re.compile(r"(?:https?|ssh|git)://(?:[A-Za-z0-9._~%-]+@)?[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~%+-]+)+/?")
_SCP = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~%+-]+(?:/[A-Za-z0-9._~%+-]+)*")
_BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/+-]{0,199}")


def parse(value) -> tuple[dict | None, str]:
    """(the change as {"repo", "branch", "head"}, "") for a valid `published` entry, else (None, why).
    (None, "") when there is none."""
    if value is None or value == {} or value == "":
        return None, ""
    if not isinstance(value, dict):
        return None, "not an object with repo, branch and head"
    repo, branch, head = (value.get(k) for k in ("repo", "branch", "head"))
    if not isinstance(branch, str) or not branch.strip():
        return None, "no branch"
    if not isinstance(head, str) or not head.strip():
        return None, "no head"
    if not isinstance(repo, str) or not repo.strip():
        return None, "no repo (owner/name or a remote URL)"
    repo, branch, head = repo.strip(), branch.strip(), head.strip().lower()
    if not _HEAD.fullmatch(head):
        return None, f"head {head[:80]!r} is not a hex commit sha (7 to 64 digits)"
    if not _BRANCH.fullmatch(branch) or ".." in branch or "//" in branch \
            or branch.endswith(("/", ".", ".lock")):
        return None, f"branch {branch[:80]!r} is not a plain branch name"
    if not (_OWNER_NAME.fullmatch(repo) or _URL.fullmatch(repo) or _SCP.fullmatch(repo)):
        return None, f"repo {repo[:80]!r} is neither owner/name nor a remote URL without credentials"
    return {"repo": repo, "branch": branch, "head": head}, ""


def same_head(a: str, b: str) -> bool:
    """True when two full or abbreviated shas name the same commit (one is a prefix of the other)."""
    a, b = (a or "").lower(), (b or "").lower()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def heads(labels: list[str]) -> list[str]:
    """The published heads a review's labels name (HEAD_LABEL)."""
    return [lb[len(HEAD_LABEL):] for lb in labels if isinstance(lb, str) and lb.startswith(HEAD_LABEL)]


def fetch_line(ch: dict, where: str = "your own worktree") -> str:
    """How a reviewer gets exactly the published head: read-only, into a scratch clone of its own."""
    clone = (f"gh repo clone {ch['repo']} scratch -- --no-checkout" if _OWNER_NAME.fullmatch(ch["repo"])
             else f"git clone --no-checkout {ch['repo']} scratch")
    return (f"Its change lives in an external repository, not in this project's git: repo {ch['repo']}, branch "
            f"{ch['branch']}, head {ch['head']}. Fetch it read-only into a scratch clone under {where} "
            f"(`{clone}`, then `git -C scratch fetch origin {ch['branch']}` and `git -C scratch checkout --detach "
            f"{ch['head']}`) and review exactly head {ch['head']}, even if the branch has moved on (say so if it has).")


REVIEW_ONLY = ("Review only: if it passes, hand off `done`. Do not push anything, run `ttp push`, approve it for "
               "the push queue, open a pull request or merge, in that repository or this one.")
