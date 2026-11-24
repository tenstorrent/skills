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

import os
import re
from pathlib import Path
from urllib.parse import quote

MD, TEXT = "md", "text"   # markdown [text](url) for chats, relays and the web app; 'text (url)' elsewhere

# Segments never linkified: code fences, code spans, markdown links, bare URLs. Markdown links cover
# what an earlier pass of this same linkifier wrote, so a second pass links nothing twice.
PROTECTED = re.compile(
    r"```.*?```|`[^`\n]*`|\[[^\]]*\]\([^)]*\)|https?://\S+|#\d+ \(unresolved reference\)", re.S)
# owner/repo#N or repo#N.
REF = r"(?<![\w/.-])(?P<rname>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)(?<!\.)#(?P<rnum>\d+)\b"
# 'PR/pull request/issue #N': no repo named, so a repo is picked only when the project has one.
PR_PHRASE = r"(?i:\b(?:PRs?|pull requests?|issues?)\s*:?\s*)(?P<pnum>#\d+\b)"
# A tt-project id: bare '#N', or with its kind word ('task #N', 'run 20240', 'review #N', 'msg #N',
# 'ask #N'). Resolution (cfg's ref_url) decides whether it links; 'msg'/'message'/'ask' name a
# message row (an ask is a message), 'review' names the review task.
ID_REF = (r"(?i:(?:(?P<ikind>task|run|review|msg|message|ask)\b\s*:?\s*)?(?<![\w/])#(?P<inum>\d+)\b"
          r"|\b(?P<kkind>task|run|review)\s+(?P<knum>\d+)\b)")
ID_REF_RE = re.compile(ID_REF)
KIND = {"task": "task", "run": "run", "review": "task", "msg": "msg", "message": "msg", "ask": "msg"}
TABLES = {"task": "tasks", "run": "runs", "msg": "messages"}   # where each kind of id lives
# A path-like token: at least one '/', then an optional :line or :line-line suffix kept in the text.
PATH = r"(?<![\w`~/.-])(?P<p>(?:~|\.{1,2})?/?(?:[\w.+@-]+/)+[\w.+@-]+)(?P<line>:\d+(?:-\d+)?)?"


def _ticket_re(cfg: dict) -> str:
    keys = sorted({re.escape(str(k)) for k in (cfg.get("jira_keys") or []) if str(k).strip()})
    return rf"(?<![A-Za-z0-9])(?P<tkey>{'|'.join(keys)})-(?P<tnum>\d+)\b" if keys and cfg.get("jira_base") else ""


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

    g = {}   # the named groups of whichever alternative matched, None for the absent ones

    def one(m: re.Match) -> str:
        g.update(m.groupdict())
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
    if not cfg.get("jira_base"):
        keys = sorted({re.escape(str(k)) for k in (cfg.get("jira_keys") or []) if str(k).strip()})
        for m in re.finditer(r"(?<![A-Za-z0-9])(" + "|".join(keys) + r")-\d+\b", text):
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
        if ref and not ref(n, KIND.get(word, word)):
            out.append(f"{m.group(0)}: no task, run or message of this project; if it is a PR or "
                       f"issue, name its repo")
        taken.append(m.span())
    return out


# --- wiring: what each project's messages link to -------------------------------------------------

SECRET_RE = re.compile(r"secret|credential|passwd|password|token|\.pem$|\.key$|\.env$|id_rsa|\.p12$|\.pfx$", re.I)
_GIT_CACHE: dict[tuple, dict] = {}   # (repo root, sha, remote) -> {"files": set, "base": str, "single": str}


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
            return f"{web}&file={quote(str(q))}"
        return None

    def is_file(path: str) -> bool:
        return bool((repo and path in (repo["files"] or set())) or readable(p, path))

    return {"jira_base": str(links.get("jira_base") or "").rstrip("/"),
            "jira_keys": [str(k) for k in (links.get("jira_keys") or [])],
            "repos": links.get("repos") or {},
            "single_repo": (repo or {}).get("single") or str(links.get("repo") or ""),
            "file_url": file_url, "is_file": is_file, "ref_ok": exists, "ref_url": _ref_url(p, web, exists)}

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


def _ref_url(p, web, exists):
    """A project id -> its web app page, None when the id names no record (never a guessed page)."""
    if not web:
        return None   # no page to link to: leave ids as written rather than label every one wrong
    def ref(n: int, kind: str):
        k = KIND.get(kind, kind) or "task"   # a bare '#N' pages as the task it names
        return f"{web}&{k}={n}" if exists(n, kind) else None
    return ref


def readable(p, path: str) -> Path | None:
    """The file `path` names, inside the project tree or the tt-project home, never a secret (the
    web app's file view serves what this returns, behind its access check)."""
    q = Path(path).expanduser()
    if not q.is_absolute():
        q = Path(p.root) / q
    try:
        q = q.resolve()
        roots = [Path(p.root).resolve(), (Path.home() / ".tt-project").resolve()]
        if not q.is_file() or not any(str(q).startswith(str(r) + os.sep) for r in roots):
            return None
    except OSError:
        return None
    return None if SECRET_RE.search(q.name) else q


def _web_base(p, db) -> str | None:
    """The web app's own link (its pages hang off its token fragment), None when there is none.
    links.web_base is the address the user opens the web app at, when it differs from what the
    web app itself reports."""
    try:
        from .web import token
        set_ = str((p.config().get("links") or {}).get("web_base") or "").rstrip("/")
        if set_:
            return f"{set_}/#token={token(p)}"
        from . import weblink
        return weblink.link(p, db)
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
    tip = targets_tip(repo, ref) if ref else ""
    key = (str(repo), ref, tip, str(links.get("repo") or ""))
    if key not in _GIT_CACHE:
        sha = ""
        base = files = single = ""
        if ref:
            try:
                tip = targets_tip(repo, ref)   # the pushed commit: only a pushed path has a public blob
                sha = "" if not tip else _git(repo, "rev-parse", "--verify", "-q", tip).stdout.strip()
            except Exception:
                sha = ""
        if sha:
            out = _git(repo, "ls-tree", "-r", "--name-only", sha)
            files = set(out.stdout.split()) if out.returncode == 0 else set()
            single = _github_repo(p, links)
            base = f"https://github.com/{single}/blob/{sha}/" if single else ""
        _GIT_CACHE[key] = {"files": files, "base": base, "single": single}
    return _GIT_CACHE[key]


def _github_repo(p, links: dict) -> str:
    """The project's own 'owner/repo' from its remotes, else links.repo (the remote may not be
    GitHub itself, e.g. a relay)."""
    from .push import _git
    out = _git(Path(p.root), "remote", "get-url", "--push", "--all")
    for url in out.stdout.split("\n"):
        if m := re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", url.strip()):
            return f"{m.group(1)}/{m.group(2)}"
    return str(links.get("repo") or "")


def for_user(p, text: str, fmt: str = MD) -> str:
    """One message to the user, linkified (the outgoing boundary callers use)."""
    try:
        return linkify(text, config(p), fmt)
    except Exception:   # a linkifier fault must never lose a message
        return text


def midnight(p) -> float:
    """The start of today in the project's home zone, for the check's message sample."""
    from datetime import datetime
    from . import timefmt
    now = datetime.now(timefmt.zone(p))
    return datetime(now.year, now.month, now.day, tzinfo=timefmt.zone(p)).timestamp()
