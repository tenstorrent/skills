# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Clickable links for every ticket, PR/issue, file path and project id a message to the user
names, model-free: the user never has to look a reference up by hand.

linkify(text, cfg, fmt) turns each reference into a link: a configured Jira key KEY-N links to the
Jira base's browse page; owner/repo#N and repo#N (a configured short name) link to the GitHub issue
URL (GitHub forwards a PR); 'PR/pull request/issue #N' links only when the project has exactly one
GitHub repo; a path in the project's repo that exists at the pushed commit links to the GitHub blob
at that commit, one on the project host to the web app's file view. A '#N' (bare or with a kind
word: 'task', 'run', 'review', 'msg') is a tt-project id: it links to the web app's page for that
task, run or message, and one that resolves to none is labelled, never left bare. Existing URLs,
markdown links, code spans and code fences are left alone, so a second pass links nothing twice.
Stored records stay raw; callers linkify only at the outgoing boundary (the listener's line, the
web app's message list and asks, `ttp status`).
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

MD, TEXT = "md", "text"   # markdown [text](url) for chats, relays and the web app; 'text (url)' elsewhere

# Segments never linkified: code fences, code spans, markdown links, bare URLs, and 'word (url)'.
# The last two cover what an earlier pass of this same linkifier wrote in either shape, so a second
# pass links nothing twice.
PROTECTED = re.compile(
    r"```.*?```|`[^`\n]*`|\[[^\]]*\]\([^)]*\)|https?://\S+|\S+ \(https?://[^)\s]+\)"
    r"|#\d+ \(unresolved reference\)", re.S)
# owner/repo#N or repo#N.
REF = r"(?<![\w/.-])(?P<rname>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)(?<!\.)#(?P<rnum>\d+)\b"
# 'PR/pull request/issue #N': no repo named, so a repo is picked only when the project has one.
PR_PHRASE = r"(?i:\b(?:PRs?|pull requests?|issues?)\s*:?\s*)(?P<pnum>#\d+\b)"
# A tt-project id: bare '#N', or with its kind word ('task #N', 'run 20240', 'review #N', 'msg #N',
# 'ask #N'). Resolution (cfg's ref_url) decides whether it links; 'msg'/'message'/'ask' name a
# message row (an ask is a message), 'review' names the review task. Without the '#' a kind word
# takes only a 3+ digit id: 'run 4 at a time' and 'task 2 of 5' are counts.
ID_REF = (r"(?i:(?:(?P<ikind>task|run|review|msg|message|ask)\b\s*:?\s*)?(?<![\w/])#(?P<inum>\d+)\b"
          r"|\b(?P<kkind>task|run|review)\s+(?P<knum>\d{3,})\b)")
ID_REF_RE = re.compile(ID_REF)
KIND = {"task": "task", "run": "run", "review": "task", "msg": "msg", "message": "msg", "ask": "msg"}
TABLES = {"task": "tasks", "run": "runs", "msg": "messages"}   # where each kind of id lives
# A path-like token: at least one '/', then an optional :line or :line-line suffix kept in the text.
PATH = r"(?<![\w`~/.-])(?P<p>(?:~|\.{1,2})?/?(?:[\w.+@-]+/)+[\w.+@-]+)(?P<line>:\d+(?:-\d+)?)?"


def _key_alts(cfg: dict) -> str:
    """The configured Jira keys as one escaped alternation, '' when there are none."""
    return "|".join(sorted({re.escape(str(k)) for k in (cfg.get("jira_keys") or []) if str(k).strip()}))


def _ticket_re(cfg: dict) -> str:
    alts = _key_alts(cfg)
    return rf"(?<![A-Za-z0-9])(?P<tkey>{alts})-(?P<tnum>\d+)\b" if alts and cfg.get("jira_base") else ""


def repo_url(owner_repo: str, n: int) -> str:
    return f"https://github.com/{owner_repo}/issues/{n}"


def _fmt(text: str, url: str, fmt: str) -> str:
    return f"[{text}]({url})" if fmt == MD else f"{text} ({url})"


def linkify(text: str, cfg: dict, fmt: str = MD) -> str:
    """`text` with every reference `cfg` can resolve turned into a link of `fmt` shape. Pure: cfg is
    plain values plus `file_url(path) -> url | None` and `is_file(path) -> bool` (a project file even
    when no link exists for it)."""
    ticket = _ticket_re(cfg)
    repos = {str(k).lower(): v for k, v in (cfg.get("repos") or {}).items()}
    single = str(cfg.get("single_repo") or "")
    file_url = cfg.get("file_url")
    ref = cfg.get("ref_url")

    def one(m: re.Match) -> str:
        g = m.groupdict()   # whichever alternative matched, None for the absent groups
        if g.get("tkey") is not None:
            return _fmt(m.group(0), f"{cfg['jira_base']}/browse/{g['tkey']}-{g['tnum']}", fmt)
        if g.get("rname") is not None:
            owner_repo = m.group("rname") if "/" in m.group("rname") else repos.get(m.group("rname").lower())
            return _fmt(m.group(0), repo_url(owner_repo, int(g["rnum"])), fmt) if owner_repo else m.group(0)
        if g.get("pnum") is not None:
            return _fmt(m.group(0), repo_url(single, int(g["pnum"].lstrip("#"))), fmt) if single else m.group(0)
        if g.get("inum") is not None or g.get("knum") is not None:
            n = int(g["inum"] or g["knum"])
            word = (g.get("ikind") or g.get("kkind") or "").lower()
            url = ref(n, KIND.get(word, word)) if ref else None
            if url:
                return _fmt(m.group(0), url, fmt)
            # a kinded id keeps its own label; a bare one gets one, so it is never left bare
            return m.group(0) if (word or ref is None) else f"{m.group(0)} (unresolved reference)"
        url = file_url(g["p"]) if file_url else None
        return _fmt(m.group(0), url, fmt) if url else m.group(0)

    # One pass, ref before path, so 'owner/repo#N' never reads as a path and a link this pass writes
    # is never re-read.
    scan = re.compile("|".join(x for x in (ticket, REF, PR_PHRASE, ID_REF, PATH) if x))
    parts, last = [], 0
    for guard in PROTECTED.finditer(text):
        parts.append(scan.sub(one, text[last:guard.start()]))
        parts.append(guard.group(0))
        last = guard.end()
    parts.append(scan.sub(one, text[last:]))
    return "".join(parts)


def problems(text: str, cfg: dict) -> list[str]:
    """Why `text` leaves a reference unlinked; one line per finding, the reference named. An id
    reference that resolves to no task, run or message is a PR or issue without its repo named:
    the repo is asked for, never guessed."""
    out: list[str] = []
    alts = _key_alts(cfg)
    if not cfg.get("jira_base") and alts:
        for m in re.finditer(r"(?<![A-Za-z0-9])(" + alts + r")-\d+\b", text):
            out.append(f"{m.group(0)}: set links.jira_base to link it")
    taken: list[tuple[int, int]] = []   # spans a plain '#N' or path must not be re-read from
    for m in re.finditer(REF, text):
        if "/" not in m.group("rname") and not (cfg.get("repos") or {}).get(m.group("rname").lower()):
            out.append(f"{m.group(0)} names no configured repo (links.repos)")
        taken.append(m.span())
    for m in re.finditer(PR_PHRASE, text):
        if not cfg.get("single_repo"):
            out.append(f"{m.group(0).strip()}: no single GitHub repo to link it to")
        taken.append(m.span())
    file_url = cfg.get("file_url")
    for m in re.finditer(PATH, text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        if cfg.get("is_file") and cfg["is_file"](m.group("p")) and not (file_url and file_url(m.group("p"))):
            out.append(f"project file {m.group('p')} has no link (no web app file view)")
    ref = cfg.get("ref_ok") or cfg.get("ref_url")
    for m in ID_REF_RE.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        n = int(m.group("inum") or m.group("knum"))
        word = (m.group("ikind") or m.group("kkind") or "").lower()
        if not ref or not ref(n, KIND.get(word, word)):
            out.append(f"{m.group(0)}: no task, run or message of this project; if it is a PR or "
                       f"issue, name its repo")
        taken.append(m.span())
    return out


# --- wiring: what each project's messages link to -------------------------------------------------

SECRET_RE = re.compile(r"secret|credential|passwd|password|token|\.pem$|\.key$|\.env$|id_rsa|\.p12$|\.pfx$", re.I)
_GIT_CACHE: dict[str, tuple] = {}   # repo root -> ((sha, links.repo), {"files": set, "base": str, "single": str}): the latest only


def config(p, db=None) -> dict:
    """The `cfg` linkify() reads, from the project's settings and repo. The git lookups are cached
    for the process; a settings change is picked up on the next start."""
    links = p.config().get("links") or {}
    repo = _git_cfg(p, links)
    web = _web_base(p, db)
    exists = _exists(p, db)

    def file_url(path: str) -> str | None:
        if (repo and repo["files"] and path in repo["files"] and repo["base"]):
            return repo["base"] + path
        q = readable(p, path)
        if q and web:
            return f"{web}#file={quote(str(q))}"
        return None

    def is_file(path: str) -> bool:
        return bool((repo and path in (repo["files"] or set())) or readable(p, path))

    return {"jira_base": str(links.get("jira_base") or "").rstrip("/"),
            "jira_keys": [str(k) for k in (links.get("jira_keys") or [])],
            "repos": links.get("repos") or {},
            "single_repo": (repo or {}).get("single") or str(links.get("repo") or ""),
            "file_url": file_url, "is_file": is_file, "ref_ok": exists, "ref_url": _ref_url(web, exists)}

def _exists(p, db):
    """Whether id `n` names a record of `kind`; a bare '#N' names a task (a run or message id
    says its kind). `db` is the caller's own connection: p.db belongs to the thread that opened it."""
    def exists(n: int, kind: str) -> bool:
        try:
            k = KIND.get(kind, kind) or "task"
            return bool((db or p.db).one(f"SELECT 1 FROM {TABLES[k]} WHERE id=?", (n,)))
        except Exception:   # a db fault never breaks a message
            return False
    return exists


def _ref_url(web, exists):
    """A project id -> its web app page, None when the id names no record (never a guessed page)."""
    if not web:
        return None   # no page to link to: leave ids as written rather than label every one wrong
    def ref(n: int, kind: str):
        k = KIND.get(kind, kind) or "task"   # a bare '#N' pages as the task it names
        return f"{web}#{k}={n}" if exists(n, kind) else None
    return ref


def readable(p, path: str) -> Path | None:
    """The file `path` names when it is one the web app's file view may serve: a file git tracks in
    the project's tree, or a run's output under the state's runs folder. Never a credential file,
    the secrets store, the web token or a database, whatever lists it."""
    from . import credguard, project
    from .push import _git
    from .web import token_path
    q = Path(path).expanduser()
    if not q.is_absolute():
        q = Path(p.root) / q
    try:
        q = q.resolve()
        root, runs = Path(p.root).resolve(), (Path(p.state) / "runs").resolve()
        deny = {Path(x).resolve() for x in (*credguard.credential_files(), project.secrets_path(), token_path(p))}
        if not q.is_file() or q in deny or q.suffix == ".db" or SECRET_RE.search(q.name):
            return None
        if q.is_relative_to(runs) and len(q.relative_to(runs).parts) >= 2:
            return q
        if q.is_relative_to(root) and _git(root, "ls-files", "--error-unmatch", "--",
                                           str(q.relative_to(root))).returncode == 0:
            return q
    except (OSError, ValueError):
        pass
    return None


def _web_base(p, db) -> str | None:
    """The web app's address, never its token: a link reaches chats, alerts and `ttp status`, which
    workers read, and the browser already holds the token. None when there is no web app.
    links.web_base is the address the user opens the web app at, when it differs from what the
    web app itself reports."""
    try:
        set_ = str((p.config().get("links") or {}).get("web_base") or "").rstrip("/")
        if set_:
            return f"{set_}/"
        from . import weblink
        return weblink.page(p, db)
    except Exception:
        return None


def _git_cfg(p, links: dict) -> dict | None:
    """The repo's file list at the pushed commit, the blob base for links to it, and the project's
    own GitHub repo, cached per process; None when the project has no git repo or push branch."""
    from .push import _git, targets_tip
    repo = Path(p.root)
    if _git(repo, "rev-parse", "--show-toplevel").returncode != 0:
        return None
    ref = str((p.config().get("delivery") or {}).get("push_branch") or "")
    try:
        tip = targets_tip(repo, ref) if ref else ""   # the pushed commit: only a pushed path has a public blob
    except Exception:
        tip = ""
    sha = _git(repo, "rev-parse", "--verify", "-q", tip).stdout.strip() if tip else ""
    key = (sha, str(links.get("repo") or ""))   # the commit, not the ref name: the push branch moves
    hit = _GIT_CACHE.get(str(repo))
    if not hit or hit[0] != key:
        base = files = single = ""
        if sha:
            out = _git(repo, "ls-tree", "-r", "--name-only", sha)
            files = set(out.stdout.split()) if out.returncode == 0 else set()
            single = _github_repo(p, links)
            base = f"https://github.com/{single}/blob/{sha}/" if single else ""
        hit = _GIT_CACHE[str(repo)] = (key, {"files": files, "base": base, "single": single})
    return hit[1]


def _github_repo(p, links: dict) -> str:
    """The project's own 'owner/repo' from its remotes, else links.repo (the remote may not be
    GitHub itself, e.g. a relay)."""
    from .push import _git
    from .watchers import _REPO_RE
    out = _git(Path(p.root), "remote", "-v")   # "<name>\t<url> (push)" per remote
    for line in out.stdout.split("\n"):
        w = line.split()
        if len(w) == 3 and w[2] == "(push)" and (m := _REPO_RE.search(w[1])):
            return m.group(1)
    return str(links.get("repo") or "")


def for_user(p, text: str, fmt: str = MD, cfg: dict | None = None) -> str:
    """One message to the user, linkified (the outgoing boundary callers use). `cfg`: a config()
    the caller built once for many messages."""
    try:
        return linkify(text, config(p) if cfg is None else cfg, fmt)
    except Exception:   # a linkifier fault must never lose a message
        return text


def midnight(p) -> float:
    """The start of today in the project's home zone, for the check's message sample."""
    from datetime import datetime
    from . import timefmt
    now = datetime.now(timefmt.zone(p))
    return datetime(now.year, now.month, now.day, tzinfo=timefmt.zone(p)).timestamp()
