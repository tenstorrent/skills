# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Built-in, model-free watchers. Each reports only CHANGES, so an unchanged world costs nothing.

- `prs`: every pull request a task opened — draft/ready, CI result, new review activity, approval,
  merge. Emits events straight to the coordinator (they are already specific and actionable).
  A PR that is out of draft without the user's recorded approval (prguard) is put back in draft
  with the daemon's own gh, outside any run, at most once an hour per PR. It also raises a high
  alert, which clears once it is back in draft, closed, merged or approved. Open PRs' findings
  (failing CI, bot review comments neither fixed nor answered) are recorded for prguard and are
  work: the coordinator hears when a PR has some (to queue a fix task) and when it is clean (to ask
  the user for a review).
- `logfile`: tails files named in the payload and screens new lines (rules, then Jev if enabled).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from . import prguard

DAY = 86400.0
UNDO_EVERY_S = 3600.0   # pr-watch puts a PR back in draft at most this often


def run_builtin(daemon, name: str, payload: dict) -> str:
    kind = payload.get("builtin") or name
    if kind == "prs":
        return watch_prs(daemon)
    if kind == "logfile":
        return watch_logs(daemon, payload)
    return f"unknown builtin {kind}"


def _gh(args: list[str], cwd: str) -> dict | None:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def _undo_ready(url: str, cwd: str) -> bool:
    """Put a PR back in draft. The daemon calls the gh on its own PATH, outside any run."""
    try:
        r = subprocess.run(["gh", "pr", "ready", url, "--undo"], capture_output=True, text=True, timeout=60,
                           cwd=cwd, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


TTP_MARKER = "<!-- ttp -->"


def _own(c: dict, pr: dict) -> bool:
    """A comment or review this project's runs posted: it carries the hidden marker and comes from the
    account that opened the PR (the runs' gh). A marker from anyone else does not hide their comment."""
    login = (c.get("author") or {}).get("login", "")
    return TTP_MARKER in str(c.get("body") or "") and bool(login) and login == (pr.get("author") or {}).get("login")


def pr_signature(pr: dict) -> dict:
    checks = pr.get("statusCheckRollup") or []
    states = sorted({(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in checks})
    failing = sorted(c.get("name") or c.get("context") or "?" for c in checks
                     if (c.get("conclusion") or c.get("state") or "").upper() in ("FAILURE", "ERROR", "TIMED_OUT",
                                                                                 "CANCELLED", "ACTION_REQUIRED"))
    pending = any((c.get("status") or c.get("state") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")
                  for c in checks)
    human = [c for c in (pr.get("comments") or []) + (pr.get("reviews") or [])
             if not ((c.get("author") or {}).get("login", "").endswith("[bot]")) and not _own(c, pr)]
    activity = hashlib.sha1(json.dumps([(c.get("author") or {}).get("login", "") + str(c.get("body", ""))[:200]
                                        + str(c.get("state", "")) for c in human]).encode()).hexdigest()[:12]
    return {"state": pr.get("state"), "draft": pr.get("isDraft"), "decision": pr.get("reviewDecision"),
            "mergeable": pr.get("mergeable"), "checks": "pending" if pending else ("failing" if failing else
                                                                                  ("passing" if states else "none")),
            "failing": failing[:10], "activity": activity, "n_human": len(human)}


def watch_prs(daemon) -> str:
    db, root = daemon.p.db, str(daemon.p.root)
    rows = db.q("SELECT id, title, pr_url, status FROM tasks WHERE pr_url IS NOT NULL AND pr_url!='' "
                "AND status NOT IN ('cancelled')")
    seen = db.kv("pr_signatures", {})
    # PRs already out of draft when this check first ran predate the guard and are not flagged.
    before = db.kv(prguard.PREDATES_KEY)
    if before is None:
        before = [u for u, s in seen.items() if s.get("state") == "OPEN" and s.get("draft") is False]
    before = set(before)
    flagged = dict(db.kv(prguard.UNAPPROVED_KEY, {}) or {})   # a PR gh could not read keeps its flag
    findings = dict(db.kv(prguard.FINDINGS_KEY, {}) or {})
    heads = dict(db.kv(prguard.HEADS_KEY, {}) or {})
    changed = 0
    for t in rows:
        pr = _gh(["pr", "view", t["pr_url"], "--json", "state,isDraft,mergeable,reviewDecision,statusCheckRollup,"
                  "comments,reviews,url,title,author,headRefOid"], root)
        if pr is None:
            continue
        key = prguard.pr_key(pr.get("url") or t["pr_url"])
        if key and pr.get("headRefOid") and (heads.get(key) or {}).get("sha") != pr["headRefOid"]:
            heads[key] = {"sha": pr["headRefOid"], "seen": time.time()}   # what an approval binds to
        sig = pr_signature(pr)
        _check_unapproved(daemon, t, pr, sig, before, flagged)
        _check_findings(daemon, t, pr, sig, findings, root)
        old = seen.get(t["pr_url"])
        if sig == old:
            continue
        seen[t["pr_url"]] = sig
        changed += 1
        if old is None:
            continue   # first sighting records a baseline; nothing new has happened yet
        what = [f"{k}: {old.get(k)} → {v}" for k, v in sig.items() if old.get(k) != v and k != "activity"]
        if sig["activity"] != old.get("activity"):
            what.append(f"new review/comment activity ({sig['n_human']} human items)")
        sev = "high" if sig["state"] == "MERGED" or sig["decision"] == "CHANGES_REQUESTED" else "normal"
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "pr", "pr_changed", sev,
              f"PR for task #{t['id']} ({pr.get('url')}): " + "; ".join(what), "queued", t["id"]))
    # A flag whose task is gone (cancelled, say) or whose PR gh has not read for a day is dropped,
    # so its alert clears.
    tasks, now = {t["id"] for t in rows}, time.time()
    flagged = {k: v for k, v in flagged.items()
               if v.get("task") in tasks and now - float(v.get("seen") or v.get("since") or 0) < DAY}
    db.set_kv("pr_signatures", seen)
    db.set_kv(prguard.PREDATES_KEY, sorted(before))
    db.set_kv(prguard.UNAPPROVED_KEY, flagged)
    db.set_kv(prguard.FINDINGS_KEY, findings)
    db.set_kv(prguard.HEADS_KEY, heads)
    unapproved = f", {len(flagged)} out of draft unapproved" if flagged else ""
    return f"ok ({len(rows)} PRs, {changed} changed{unapproved})"


def _check_unapproved(daemon, t: dict, pr: dict, sig: dict, before: set, flagged: dict) -> None:
    """Handle a PR that is out of draft without the user's recorded approval: put it back in draft
    (at most once an hour per PR), raise a high alert for the user and, the first time, queue an
    event for the coordinator. The flag (and the alert) clears once the PR is back in draft,
    closed, merged or approved."""
    db, url = daemon.p.db, t["pr_url"]
    key = prguard.pr_key(pr.get("url") or url)
    if not key:
        return
    if sig["state"] != "OPEN" or sig["draft"] is not False:
        before.discard(url)
        flagged.pop(key, None)
        if sig["draft"]:
            prguard.drop_spent(db, key)   # back in draft: leaving it again needs a fresh yes
        return
    if url in before or prguard.approved(db, key):
        flagged.pop(key, None)
        return
    shown, now = pr.get("url") or url, time.time()
    tried = db.kv(prguard.UNDONE_KEY, {}) or {}
    undone = False
    if now - float(tried.get(key, 0)) >= UNDO_EVERY_S:
        tried = {k: v for k, v in tried.items() if now - float(v) < DAY}
        tried[key] = now
        db.set_kv(prguard.UNDONE_KEY, tried)   # counts the attempt before it runs, so a crash does not repeat it
        undone = _undo_ready(shown, str(daemon.p.root))
    if undone:
        said = ("pr-watch put it back in draft (gh pr ready --undo). Find out what took it out of draft. If the "
                "user approves it, record it with pr_approve before it leaves draft again.")
        told = "It was put back in draft. If it should be ready for review, say so."
    else:
        said = ("pr-watch did not put it back in draft this time (it did less than an hour ago, or gh failed). "
                "Put it back in draft (gh pr ready --undo) unless the user approves it; on their yes, "
                "record it with pr_approve.")
        told = "The coordinator puts it back in draft unless you approve it."
    if key not in flagged:
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "pr", "pr_unapproved_ready", "high",
              f"PR for task #{t['id']} ({shown}) is out of draft, but the user's approval is not on record. "
              + said, "queued", t["id"]))
    flagged[key] = {"task": t["id"], "url": shown, "since": flagged.get(key, {}).get("since") or now,
                    "seen": now, **({"undone": now} if undone else {})}
    daemon.alert(f"{prguard.UNAPPROVED_ALERT}:{key}",
                 f"{shown} (task #{t['id']}) left draft without your recorded approval. " + told, "high",
                 every_s=DAY)


BOT_QUERY = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
             "pullRequest(number:$number){"
             "comments(last:100){nodes{author{__typename login} createdAt url}}"
             "reviews(last:100){nodes{author{__typename login} state body submittedAt url}}"
             "reviewThreads(first:100){nodes{isResolved isOutdated "
             "comments(first:50){nodes{author{__typename login} url}}}}}}}")


def _is_bot(author: dict | None) -> bool:
    a = author or {}
    return a.get("__typename") == "Bot" or str(a.get("login", "")).endswith("[bot]")


def bot_open(data: dict) -> list[str]:
    """URLs of bot review findings on a PR that are neither fixed nor answered: a review thread a bot
    started that is unresolved, not outdated and has no reply from a person; a bot's top-level
    comment, or review asking for changes, with no comment from a person after it."""
    pr = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    out = []
    for th in (pr.get("reviewThreads") or {}).get("nodes") or []:
        cs = (th.get("comments") or {}).get("nodes") or []
        if cs and _is_bot(cs[0].get("author")) and not th.get("isResolved") and not th.get("isOutdated") \
                and all(_is_bot(c.get("author")) for c in cs[1:]):
            out.append(cs[0].get("url") or "?")
    comments = (pr.get("comments") or {}).get("nodes") or []
    human_times = sorted(c.get("createdAt") or "" for c in comments if not _is_bot(c.get("author")))
    last_human = human_times[-1] if human_times else ""
    items = [(c.get("createdAt") or "", c.get("url")) for c in comments if _is_bot(c.get("author"))]
    items += [(r.get("submittedAt") or "", r.get("url")) for r in (pr.get("reviews") or {}).get("nodes") or []
              if _is_bot(r.get("author")) and r.get("state") == "CHANGES_REQUESTED"]
    out += [u or "?" for ts, u in items if ts > last_human]
    return out


def _check_findings(daemon, t: dict, pr: dict, sig: dict, findings: dict, root: str) -> None:
    """Record an open PR's findings and tell the coordinator when they change: CI failing or bot
    review comments open is work (a fix task); CI green with every bot comment fixed or answered is
    when it may ask the user for a review."""
    db, url = daemon.p.db, pr.get("url") or t["pr_url"]
    key = prguard.pr_key(url)
    if not key:
        return
    if sig["state"] != "OPEN":
        findings.pop(key, None)
        return
    owner, rest = key.split("/", 1)
    name, number = rest.split("#")
    data = _gh(["api", "graphql", "-f", f"query={BOT_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
                "-F", f"number={number}"], root)
    old = findings.get(key) or {}
    bots = bot_open(data) if data is not None else old.get("bot", [])   # unread: keep what was known
    rec = {"task": t["id"], "url": url, "failing": sig["failing"], "pending": sig["checks"] == "pending",
           "bot_open": len(bots), "bot": bots[:10], "notified": old.get("notified")}
    findings[key] = rec
    if rec["pending"]:
        return
    dirty = bool(rec["failing"] or rec["bot_open"])
    mark = json.dumps([rec["failing"], sorted(bots)]) if dirty else "clean"
    if mark == rec["notified"] or (not dirty and rec["notified"] is None):
        rec["notified"] = mark   # a PR clean when first seen needs no news: its task's hand-off said so
        return
    rec["notified"] = mark
    if dirty:
        what = "; ".join([*(["CI failing: " + ", ".join(rec["failing"])] if rec["failing"] else []),
                          *([f"{len(bots)} bot review comment(s) neither fixed nor answered: "
                             + ", ".join(bots[:5])] if bots else [])])
        text = (f"PR for task #{t['id']} ({url}) has open findings: {what}. This is work: queue a code task "
                f"(on the PR's branch) to fix or answer each and get CI green. Do not ask the user to review "
                f"it until pr-watch reports it clean.")
    else:
        text = (f"PR for task #{t['id']} ({url}) is clean: CI green and every bot review comment fixed or "
                f"answered. If its review task passed, ask the user for a draft review now (ask_user, "
                f"blocking review, with its URL).")
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
         (time.time(), "pr", "pr_findings" if dirty else "pr_clean", "normal", text, "queued", t["id"]))


def watch_logs(daemon, payload: dict) -> str:
    db = daemon.p.db
    offsets = db.kv("log_offsets", {})
    n = 0
    for pattern in payload.get("files", []):
        for path in sorted(Path(daemon.p.root).glob(pattern)) if not os.path.isabs(pattern) else [Path(pattern)]:
            if not path.is_file():
                continue
            key = str(path)
            size = path.stat().st_size
            start = offsets.get(key, size)       # first sighting starts at the end: history is not news
            if size < start:
                start = 0                       # rotated or truncated
            if size > start:
                with open(path, "rb") as f:
                    f.seek(start)
                    chunk = f.read(min(size - start, 256 * 1024)).decode(errors="replace")
                offsets[key] = start + len(chunk.encode())
                lines = [ln for ln in chunk.splitlines() if ln.strip()]
                interesting = [ln for ln in lines if daemon_rule(ln) != "info"]
                for ln in interesting[:20]:
                    daemon.observe(f"log:{path.name}", ln[:2000])
                    n += 1
            else:
                offsets[key] = size
    db.set_kv("log_offsets", offsets)
    return f"ok ({n} lines screened)"


def daemon_rule(line: str) -> str:
    from .screen import rule_severity
    return rule_severity(line)
