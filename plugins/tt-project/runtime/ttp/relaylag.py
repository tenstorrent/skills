# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A push remote that is a relay, and how far the real upstream is behind it. Model-free.

Some projects push to a relay that a forwarder on another machine sends on to the real upstream.
While the forwarder is down, pushes land on the relay only, and the push checks and `ttp push audit`
(which read the push remote) still say landed. `delivery.push_upstream` names that upstream: a URL
readable without credentials (https, git, file or a local path) or a git remote's name, as a string
(the push branch's name there) or {"url", "branch"}. Unset: off.

The daemon (Daemon.check_relay_lag) asks the upstream for its branch tip at most every EVERY_S with
one anonymous `git ls-remote` in a thread (no credential helper, no prompt, never ssh), and compares
it with the relay's tip as the local tracking ref has it (refs/remotes/<remote>/<branch>; a push
moves it). The lag is the number of commits on the relay tip not in the upstream tip (git rev-list);
an upstream tip the local repository does not have, or an upstream that cannot be read, is `unknown`,
never 0. Since when: the time the oldest relay tip still not upstream was
first seen (relay tips are remembered while the lag lasts). The answer is kept in kv KV, so status,
the daily review, `ttp push audit`, the landing replies and post_land events read it without network.

One high alert (KEY) opens once the lag has lasted relay.gone_after_s, and clears when the lag is 0
(alerts.holds asks `holds`). An unknown answer neither opens nor clears it."""
from __future__ import annotations

import os
import re
import threading
import time

from . import ghanon, landed

KV = "relay_lag"           # kv: the last answer (see measure)
KEY = "relay-behind"       # alert key
EVERY_S = 300.0            # between upstream reads
LS_TIMEOUT_S = 20.0
TIPS_MAX = 200             # relay tips remembered while the lag lasts
OK, AHEAD, UNKNOWN = "ok", "ahead", "unknown"
_ANON = re.compile(r"^(https?|git|file)://", re.I)
_jobs: dict[str, dict] = {}   # project root -> {"thread", "answer", "started"}


def upstream(p, cfg: dict) -> dict | None:
    """{"url", "branch", "remote", "relay_branch"} of delivery.push_upstream, None when off or no push branch."""
    raw = (cfg.get("delivery") or {}).get("push_upstream")
    if isinstance(raw, dict):
        url, branch = str(raw.get("url") or "").strip(), str(raw.get("branch") or "").strip()
    else:
        url, branch = str(raw or "").strip(), ""
    if not url:
        return None
    from . import push
    try:
        remote, relay_branch = push.target(p, p.root)
    except ValueError:
        return None
    return {"url": url, "branch": branch or relay_branch, "remote": remote, "relay_branch": relay_branch}


def _anon_url(p, url: str) -> tuple[str, str]:
    """(url to read, "") or ("", why not): a remote's name is read as its URL; ssh is never used."""
    if not (_ANON.match(url) or url.startswith("/")):
        r = landed._git(p.root, "remote", "get-url", url, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            url = r.stdout.strip()
    if re.match(r"[a-z+]+://[^/]*@", url, re.I):
        return "", "the upstream URL carries a user or credentials; give one readable anonymously"
    if _ANON.match(url) or (url.startswith("/") and os.path.exists(url)):
        return url, ""
    return "", "the upstream URL is not one readable without credentials (https, git, file or a path)"


def ls_remote(p, url: str, branch: str) -> tuple[str | None, str]:
    """(the upstream branch's tip, "") or (None, why): one anonymous `git ls-remote` (ghanon.ls_remote)."""
    url, why = _anon_url(p, url)
    if not url:
        return None, why
    sha, why = ghanon.ls_remote(url, f"refs/heads/{branch}", LS_TIMEOUT_S)
    return (sha, "") if sha else (None, f"upstream could not be read: {why}")


def measure(p, up: dict, answer: tuple[str | None, str], prev: dict | None, now: float) -> dict:
    """The lag record: {"at", "state", "lag" (int, None unless known), "relay", "upstream", "since", "why",
    "target", "tips"}. `answer`: what ls_remote returned. `prev`: the last record (its tips and since)."""
    prev = prev or {}
    target = f"{up['remote']}/{up['relay_branch']}"
    rec = {"at": now, "target": target, "url": up["url"], "branch": up["branch"], "lag": None, "since": None,
           "upstream": answer[0] or "", "why": answer[1], "tips": []}
    relay = landed._commit(p.root, f"refs/remotes/{target}")
    rec["relay"] = relay
    if not relay:
        return {**rec, "state": UNKNOWN, "why": f"no local tracking ref of {target}"}
    if not answer[0]:
        # Unreadable: unknown, never 0; the lag's start is kept for when the upstream answers again.
        return {**rec, "state": UNKNOWN, "since": prev.get("since"), "tips": prev.get("tips") or []}
    upsha = answer[0]
    if upsha == relay:
        return {**rec, "state": OK, "lag": 0}
    tips = [t for t in (prev.get("tips") or []) if isinstance(t, list) and len(t) == 2]
    if not tips or tips[-1][0] != relay:
        tips.append([relay, now])
    if not landed._commit(p.root, upsha):
        # Diverged, or newer than the relay: unknown, like an unread upstream.
        return {**rec, "state": UNKNOWN, "since": prev.get("since"), "tips": prev.get("tips") or [],
                "why": f"upstream tip {upsha[:10]} is not in the local repository (diverged, or newer than the relay)"}
    r = landed._git(p.root, "rev-list", "--count", f"{upsha}..{relay}", timeout=30)
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        return {**rec, "state": UNKNOWN, "why": "git rev-list failed", "since": prev.get("since"),
                "tips": prev.get("tips") or []}
    n = int(r.stdout.strip())
    if n == 0:
        return {**rec, "state": OK, "lag": 0}
    # The oldest remembered relay tip not upstream yet: when its commits were first seen on the relay.
    while len(tips) > 1 and landed.on_branch(p.root, tips[0][0], upsha):
        tips.pop(0)
    tips = tips[-TIPS_MAX:]
    return {**rec, "state": AHEAD, "lag": n, "since": tips[0][1], "tips": tips}


def state(db) -> dict | None:
    rec = db.kv(KV)
    return rec if isinstance(rec, dict) and rec.get("state") else None


def holds(db) -> bool:
    """The alert's condition: the last answer is not lag 0 (unknown keeps it, only 0 clears it)."""
    rec = state(db)
    return bool(rec) and rec["state"] != OK


def _hours(s: float) -> str:
    return f"{max(s, 0.0) / 3600:.1f} h"


def line(db, now: float | None = None, ok_too: bool = False) -> str:
    """'relay ahead of upstream by N commits for H h', an unknown line, or "" (no record; lag 0 unless
    `ok_too`)."""
    rec = state(db)
    if not rec:
        return ""
    now = time.time() if now is None else now
    t = rec.get("target") or "the push remote"
    age = f" (checked {_hours(now - float(rec.get('at') or now))} ago)"
    if rec["state"] == OK:
        return f"relay {t}: upstream has every commit{age}" if ok_too else ""
    since = rec.get("since")
    dur = f" for {_hours(now - float(since))}" if since else ""
    if rec["state"] == AHEAD:
        n = int(rec["lag"])
        return f"relay {t} ahead of upstream by {n} commit{'s' if n != 1 else ''}{dur}{age}"
    return f"relay {t} vs upstream: lag unknown ({rec.get('why') or 'not read'}){age}"


def landing_words(db) -> str:
    """What a landing reply adds while the lag is not 0: "" when 0 or off."""
    rec = state(db)
    if not rec or rec["state"] == OK:
        return ""
    return "on relay, not yet upstream" if rec["state"] == AHEAD else "on relay, upstream unknown"


def apply(p, cfg: dict, up: dict, answer: tuple[str | None, str], alert, now: float | None = None) -> dict:
    """Store the answer's lag record and raise or clear the alert."""
    from . import alerts, chatgone
    now = time.time() if now is None else now
    db = p.db
    rec = measure(p, up, answer, state(db), now)
    db.set_kv(KV, rec)
    after = float((cfg.get("relay") or {}).get("gone_after_s", chatgone.GONE_AFTER_S) or 0)
    if rec["state"] == OK:
        alerts.close_key(db, KEY, "the upstream has every relay commit",
                         text="Cleared: " + alerts.CLEARED_TEXT[KEY])
    elif rec["state"] == AHEAD and after > 0 and rec.get("since") and now - rec["since"] >= after:
        alert(KEY, f"{line(db, now)}: pushes land on the relay only; its forwarder may be down (landed work "
                   f"is not upstream yet)", severity="high")
    return rec


def tick(p, cfg: dict, alert, now: float | None = None) -> None:
    """Daemon step: apply the last upstream read, and start the next one in a thread when due."""
    now = time.time() if now is None else now
    up = upstream(p, cfg)
    key = str(p.root)
    if not up:
        if p.db.kv(KV) is not None:
            p.db.set_kv(KV, None)
            from . import alerts
            alerts.close_key(p.db, KEY, "delivery.push_upstream is off")
        _jobs.pop(key, None)
        return
    job = _jobs.setdefault(key, {"started": 0.0})
    if job.get("answer") and not (job.get("thread") and job["thread"].is_alive()):
        apply(p, cfg, up, job.pop("answer"), alert, now)
    if now - job["started"] < EVERY_S or (job.get("thread") and job["thread"].is_alive()):
        return
    job["started"] = now

    def work():
        try:
            job["answer"] = ls_remote(p, up["url"], up["branch"])
        except Exception as e:   # never 0: an error is an unknown answer
            job["answer"] = (None, f"upstream read failed: {type(e).__name__}: {e}")
    job["thread"] = threading.Thread(target=work, daemon=True, name="relay-lag")
    job["thread"].start()
