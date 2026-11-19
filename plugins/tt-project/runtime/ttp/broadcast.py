# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One user message to every project, delivered once per project (`ttp say --all`).

This machine's ledger (~/.tt-project/broadcasts.jsonl, one JSON line per event, appended under a
lock) keys a message by its normalized verbatim words (`key`): the user's words without the
relayer's context (relay.split), casefolded, punctuation dropped and whitespace joined. The same
words resent with other punctuation or line breaks get the same key.

An "open" line gives a broadcast its id (b-<12 hex>) and a "delivered" line records each project
that got it. A key reuses its id for REUSE_S, so a resend skips the projects already delivered.
A plain `ttp say <project>` records its delivery too: a user who tells the projects one at a time
builds the same record, and a later `--all` reaches only the rest.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from . import project, relay

REUSE_S = 24 * 3600     # a key resent within this reuses its broadcast id (and skips its deliveries)
KEEP_S = 7 * 24 * 3600  # lines older than this go when the ledger is compacted
COMPACT_BYTES = 256 * 1024
_PUNCT = re.compile(r"[^\w\s]+")


def path() -> Path:
    return project.HOME_DIR / "broadcasts.jsonl"


def normalize(text: str) -> str:
    """The user's verbatim words, casefolded, without punctuation, whitespace joined."""
    words, _ = relay.split(text or "")
    return " ".join(_PUNCT.sub(" ", words.casefold()).split())


def key(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


@contextlib.contextmanager
def _locked():
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(path().with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "rb+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)   # every project's `ttp say` on this machine appends here
        yield


def _read() -> list[dict]:
    try:
        raw = path().read_text(errors="replace")
    except FileNotFoundError:
        return []
    out = []
    for ln in raw.splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue    # a torn last line from a crash: the rest still counts
        if isinstance(e, dict) and e.get("id"):
            out.append(e)
    return out


def _append(*lines: dict) -> None:
    if not path().exists():     # private to this user: create it 0600 before the first append
        os.close(os.open(path(), os.O_WRONLY | os.O_CREAT, 0o600))
        project.fsync_dir(path().parent)
    project.durable_append(path(), "".join(json.dumps(e, sort_keys=True) + "\n" for e in lines))


def _compact(lines: list[dict], now: float) -> None:
    """Once the file has grown past COMPACT_BYTES, drop broadcasts last touched more than KEEP_S ago,
    else (all that recent) those more than REUSE_S ago. `lines` must be the whole file, read under the
    lock: call this before appending. No rewrite when nothing can go, so a busy day costs no rewrites."""
    try:
        if path().stat().st_size < COMPACT_BYTES:
            return
    except FileNotFoundError:
        return
    last: dict[str, float] = {}
    for e in lines:
        last[e["id"]] = max(last.get(e["id"], 0.0), float(e.get("ts") or 0))
    for age in (KEEP_S, REUSE_S):
        keep = [e for e in lines if now - last[e["id"]] < age]
        if len(keep) < len(lines):
            project.durable_write(path(), "".join(json.dumps(e, sort_keys=True) + "\n" for e in keep), mode=0o600)
            return


def _fold(lines: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in lines:
        b = out.setdefault(e["id"], {"id": e["id"], "key": e.get("key"), "norm": e.get("norm", ""),
                                     "ts": float(e.get("ts") or 0), "delivered": {}})
        if e.get("event") == "open":
            b.update(key=e.get("key"), norm=e.get("norm", ""), ts=float(e.get("ts") or 0))
        elif e.get("event") == "delivered" and e.get("project"):
            b["delivered"].setdefault(e["project"], e.get("ref") or "")
    for b in out.values():
        b["shingles"] = relay._shingles(b["norm"].split())
    return out


def _current(lines: list[dict], k: str, now: float) -> dict | None:
    """The newest broadcast of key `k` opened within REUSE_S."""
    hits = [b for b in _fold(lines).values() if b["key"] == k and now - b["ts"] < REUSE_S]
    return max(hits, key=lambda b: b["ts"]) if hits else None


def claim(text: str, now: float | None = None, bid: str | None = None) -> dict:
    """The broadcast for these words: the one opened within REUSE_S, else a new one (`bid`, the id
    another machine gave it, else a fresh id). Returns {id, key, ts, delivered: {project: ref}}."""
    now = now or time.time()
    k = key(text)
    with _locked():
        lines = _read()
        cur = _current(lines, k, now)
        if cur and (not bid or cur["id"] == bid):
            return cur
        b = {"id": bid or f"b-{secrets.token_hex(6)}", "key": k, "norm": normalize(text), "ts": now,
             "delivered": {}}
        _compact(lines, now)    # before the append: it rewrites the file from `lines`
        _append({"event": "open", "id": b["id"], "key": k, "norm": b["norm"], "ts": now})
        b["shingles"] = relay._shingles(b["norm"].split())
        return b


def record(bid: str, name: str, ref: str, now: float | None = None) -> None:
    """Project `name` got broadcast `bid` (`ref`: its message id there, or where it is queued)."""
    with _locked():
        _append({"event": "delivered", "id": bid, "project": name, "ref": str(ref), "ts": now or time.time()})


def note_delivery(text: str, name: str, ref: str, bid: str | None = None, now: float | None = None) -> str | None:
    """Record one `ttp say` delivery under its words' broadcast; the broadcast id, or None when the
    ledger could not be written (a message already delivered is never undone for that)."""
    try:
        b = claim(text, now, bid)
        record(b["id"], name, ref, now)
        return b["id"]
    except OSError:
        return None


def find(bid: str) -> dict | None:
    """Broadcast `bid` as this machine's ledger knows it, else None (it was sent from another machine)."""
    try:
        return _fold(_read()).get(bid)
    except OSError:
        return None


def label(bid: str) -> str:
    """The digest's line for an inbound message that came as broadcast `bid`."""
    b = find(bid)
    n = f" ({len(b['delivered'])} delivered)" if b and b["delivered"] else ""
    return (f"broadcast {bid}: the user sent these words to every project{n}. "
            "Do not relay them to other projects.")
