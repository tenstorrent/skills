# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider credits and free window resets: what lets an account go on past a full plan window.

A provider's rate-limit answer may say more than its window percentages:
- paid credits (`credits: {hasCredits, unlimited, balance}`): the account keeps working past the
  window, billed per use. The window then does not hold new work, but that spend counts toward the
  project's dollar caps and the global cap (budget.evaluate). `spendControlReached` holds it again;
- free window resets (`rateLimitResetCredits: {availableCount, credits: [{id, expiresAt, ...}]}`):
  one consumed resets the windows at once.

`parse` reads them from any provider's answer into one structure; a missing or malformed field is
None (unknown), never "has credits". The daemon stores the latest reading per provider (`record`),
with the account it was read for. Claude reports no credits block: its run's rate_limit_event
overage fields map to paid credits (providers.claude.credits_from_event), open only while
`overageStatus` is 'allowed'.

Auto-consume (budget.reset_credits_auto, default on) spends one free reset, earliest-expiring
first, at most once per window of a provider:
- held: a window is at the line (100 - reserve_pct) and resets more than
  budget.reset_credit_min_hours (12) from now. A window that resets sooner just waits;
- expiring: the earliest credit expires within budget.reset_credit_expiry_hours (48) and a window
  is at least budget.reset_credit_expiry_use_pct (50) % used, so a credit that would lapse unused
  is not wasted on an empty window.
Free resets belong to the provider account, which several projects on one machine may meter. The
decide-and-consume step runs under a machine-wide lock per provider and account (in TTP_HOME), reads
the meter again inside it, and keeps one machine-wide record (SHARED) of the consumes per window: no
project consumes against a window another one already consumed against.
The consume carries a fresh idempotency key, saved in that record with the credit and window BEFORE
the call, and reused on every retry, from any project: a crash or a lost answer never consumes a
second credit. A saved consume whose credit has left the provider's list is taken as done (only a
reading that lists the credits says so; one without the list says nothing).
The answers: 'reset' and 'alreadyRedeemed' (the same key reset the window already) are success;
'nothingToReset' and 'noCredit' are final, never retried, and keep that reading's free resets from
being offered until a new reading changes them; a transport error (no answer, timeout, connection,
5xx) is retried with the same key, at most MAX_TRIES times; anything else is recorded, not retried.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any

from . import project
from .db import DB

KV = "credits:"            # credits:<provider> -> the latest parsed reading, with "ts" and "account"
USED = "reset_used:"       # reset_used:<provider> -> [{credit, window, against, why, outcome, ts, reported}]
SHARED = "reset-credits-{}.json"   # in TTP_HOME, per provider account: [{state, credit, key, window, against, ...}]
LOCK = "reset-credits-{}.lock"
FRESH_S = 30 * 60          # an older reading says nothing about now
SAME_WINDOW_S = 600        # a window's reset time jitters by seconds between readings
RETRY_S = 600              # a consume that got no answer (transport error) is retried (same key) after this
MAX_TRIES = 3              # then the window gets no more tries
KEEP_USED = 20
KEEP_SHARED_S = 30 * 86400
SUCCESS = ("reset", "alreadyRedeemed")
FINAL = {"nothingToReset": "the provider had nothing to reset", "noCredit": "the account has no free reset left"}
TRANSPORT_RE = re.compile(r"time(d)? ?out|connect|network|unavailable|temporar|gateway|overloaded|\b5\d\d\b", re.I)
DEFAULTS = {"reset_credits_auto": True, "reset_credit_min_hours": 12.0, "reset_credit_expiry_hours": 48.0,
            "reset_credit_expiry_use_pct": 50.0}


def _bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> float | None:
    """An epoch time in seconds (or milliseconds), or an ISO 8601 time; None otherwise."""
    n = _num(v)
    if n is not None:
        return n / 1000 if n > 1e11 else n if n > 0 else None
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def parse(answer: Any) -> dict:
    """The credit fields of a rate-limit answer, read from the answer itself or its `rateLimits`.
    Unknown fields are None; free resets are sorted earliest-expiring first (unknown expiry last)."""
    srcs = [s for s in (answer, answer.get("rateLimits") if isinstance(answer, dict) else None)
            if isinstance(s, dict)]

    def find(key: str) -> Any:
        return next((s[key] for s in srcs if s.get(key) is not None), None)

    c = find("credits")
    c = c if isinstance(c, dict) else {}
    r = find("rateLimitResetCredits")
    r = r if isinstance(r, dict) else {}
    lst = []
    for x in r.get("credits") if isinstance(r.get("credits"), list) else []:
        if isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"] and x.get("status") in (None, "available"):
            lst.append({"id": x["id"], "expires_at": _ts(x.get("expiresAt"))})
    lst.sort(key=lambda x: (x["expires_at"] is None, x["expires_at"] or 0))
    count = r.get("availableCount")
    count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else (
        len(lst) if isinstance(r.get("credits"), list) else None)
    kind = find("rateLimitReachedType")
    return {"listed": isinstance(r.get("credits"), list),
            "paid": {"has_credits": _bool(c.get("hasCredits")), "unlimited": _bool(c.get("unlimited")),
                     "balance": _num(c.get("balance"))},
            "spend_control_reached": _bool(find("spendControlReached")),
            "reached_type": kind if isinstance(kind, str) else None,
            "free_resets": count, "resets": lst}


def paid_open(reading: dict | None) -> bool:
    """Whether paid credits let the account go on past its windows: credits or unlimited reported
    true, and no spend control reached."""
    if not reading:
        return False
    p = reading.get("paid") or {}
    return (p.get("has_credits") is True or p.get("unlimited") is True) and reading.get("spend_control_reached") is not True


def record(db: DB, provider: str, reading: dict, account: str = "", now: float | None = None) -> None:
    db.set_kv(KV + provider, {**reading, "ts": now or time.time(), "account": account or ""})


def latest(db: DB, provider: str, now: float | None = None) -> dict | None:
    """The provider's latest credit reading, or None when it is older than FRESH_S. A reading whose
    free resets the provider refused (`refused`) carries that answer in "refused"."""
    now = now or time.time()
    r = db.kv(KV + provider)
    if not isinstance(r, dict) or now - float(r.get("ts") or 0) > FRESH_S:
        return None
    no = refused(db, provider, r, r.get("account") or "", now)
    return {**r, "refused": no} if no else r


def settings(cfg: dict) -> dict:
    b = cfg.get("budget") or {}
    return {k: b.get(k, v) if b.get(k) is not None else v for k, v in DEFAULTS.items()}


def used(db: DB, provider: str) -> list[dict]:
    v = db.kv(USED + provider) or []
    return v if isinstance(v, list) else []


# the machine-wide record --------------------------------------------------------------------------
def _file(pattern: str, provider: str, account: str):
    """A machine-wide file of one provider account (the account hashed: no names in file names)."""
    digest = hashlib.sha256(f"{provider}|{account or ''}".encode()).hexdigest()[:16]
    return project.HOME_DIR / pattern.format(f"{re.sub(r'[^a-z0-9]', '', provider.lower())}-{digest}")


def shared(provider: str, account: str) -> list[dict]:
    """The machine-wide record of this account's consumes: in flight ("pending", with its key) or done."""
    try:
        v = json.loads(_file(SHARED, provider, account).read_text())
    except (OSError, ValueError):
        return []
    return [e for e in v if isinstance(e, dict)] if isinstance(v, list) else []


def _write_shared(provider: str, account: str, entries: list[dict], now: float) -> None:
    project.write_json(_file(SHARED, provider, account), [
        e for e in entries if e.get("state") == "pending" or now - float(e.get("ts") or 0) < KEEP_SHARED_S
    ][-KEEP_USED:], 0o600)


@contextlib.contextmanager
def _locked(provider: str, account: str):
    """The machine-wide lock of one provider account's free resets: one consume decision at a time."""
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(_file(LOCK, provider, account), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _same(e: dict, w) -> bool:
    return e.get("window") == w.window and bool(w.resets_at) and \
        abs(float(e.get("against") or 0) - float(w.resets_at)) < SAME_WINDOW_S


def _spent_on(u: list[dict], w) -> bool:
    return any(_same(x, w) for x in u if x.get("state") != "pending")


def _ids(reading: dict | None) -> list[str]:
    return sorted(c["id"] for c in (reading or {}).get("resets") or [])


def refused(db: DB, provider: str, reading: dict | None, account: str = "", now: float | None = None,
            windows: list | None = None) -> str:
    """The final 'no' (FINAL) the provider gave to the last consume while its free resets read as they
    do in `reading`, or '': those resets are no unblock path until a reading changes them. A
    'nothingToReset' counts only while the window it answered for is still the current one."""
    if not reading:
        return ""
    now = now or time.time()
    last = max(used(db, provider) + [e for e in shared(provider, account) if e.get("state") == "done"],
               key=lambda e: float(e.get("ts") or 0), default=None)
    if not last or last.get("outcome") not in FINAL or sorted(last.get("ids") or []) != _ids(reading):
        return ""
    if last["outcome"] == "nothingToReset":
        if windows is None:
            from .budget import plan_windows
            windows = plan_windows(db, now)
        if not any(w.provider == provider and _same(last, w) for w in windows):
            return ""
    return last["outcome"]


def decide(db: DB, cfg: dict, provider: str, windows: list, reading: dict | None, now: float | None = None,
           others: list[dict] | None = None) -> dict | None:
    """The free reset to consume now, or None: {credit, expires_at, window, against, why}. `others`:
    the machine-wide record's entries for this account (another project's consume counts too)."""
    now = now or time.time()
    s = settings(cfg)
    if not s["reset_credits_auto"] or not reading:
        return None
    creds = reading.get("resets") or []
    if not creds:
        return None
    line = 100.0 - float((cfg.get("budget") or {}).get("reserve_pct", 10))
    mine = sorted((w for w in windows if w.provider == provider and w.resets_at),
                  key=lambda w: -float(w.resets_at))
    if refused(db, provider, reading, mine[0].account if mine else "", now, mine):
        return None
    done = used(db, provider) + list(others or [])
    first = creds[0]

    def pick(w, why: str) -> dict:
        return {"credit": first["id"], "expires_at": first.get("expires_at"), "window": w.window,
                "against": float(w.resets_at), "utilization": round(w.utilization, 1), "why": why,
                "ids": _ids(reading)}
    for w in mine:
        if w.utilization >= line and w.resets_at - now > float(s["reset_credit_min_hours"]) * 3600 \
                and not _spent_on(done, w):
            return pick(w, "held")
    if first.get("expires_at") and first["expires_at"] - now <= float(s["reset_credit_expiry_hours"]) * 3600:
        for w in mine:
            if w.utilization >= float(s["reset_credit_expiry_use_pct"]) and not _spent_on(done, w):
                return pick(w, "expiring")
    return None


def transport_error(err: Any) -> bool:
    """Whether a JSON-RPC error means the request may not have reached the provider (timeout,
    connection, a 5xx status): only those are retried."""
    if isinstance(err, dict):
        for k in ("status", "statusCode", "httpStatus"):
            n = _num(err.get(k)) if err.get(k) is not None else None
            if n is not None and 500 <= n < 600:
                return True
        err = json.dumps(err)
    return bool(TRANSPORT_RE.search(str(err)))


def classify(res: dict) -> tuple[str, str]:
    """('success' | 'final' | 'transport' | 'other', what) for a provider's consume answer."""
    if res.get("transport"):
        return "transport", str(res.get("error") or "no answer")
    if res.get("error") is not None:
        return "other", str(res["error"])
    out = str(res.get("outcome") or ("reset" if res.get("ok") is True else ""))
    if out in SUCCESS:
        return "success", out
    if out in FINAL:
        return "final", out
    return "other", out or "no outcome"


ENTRY = ("credit", "expires_at", "window", "against", "utilization", "why", "ids")


def _finish(db: DB, provider: str, pick: dict, outcome: str, now: float, reason: str = "") -> dict:
    """The local record of a consume's end, kept for reporting (USED)."""
    entry = {k: pick.get(k) for k in ENTRY}
    entry.update({"outcome": outcome, "ts": now, "reported": False, **({"reason": reason} if reason else {})})
    with db.tx():
        db.set_kv(USED + provider, (used(db, provider) + [entry])[-KEEP_USED:])
    return entry


def consume(db: DB, cfg: dict, provider: str, prov: Any, windows: list, reading: dict | None,
            now: float | None = None, remeter: Any = None) -> dict | None:
    """Consume one free reset when `decide` says so, crash-safe and once per account window across
    every project on this machine. `remeter()` -> (windows, reading) reads the provider again inside
    the lock. Returns the finished entry ({..., "outcome"}), {"error": ...} while a transport error
    will be retried, or None."""
    now = now or time.time()
    account = next((w.account for w in windows if w.provider == provider and w.account), "") \
        or (reading or {}).get("account") or ""
    if not any(e.get("state") == "pending" for e in shared(provider, account)) and \
            not decide(db, cfg, provider, windows, reading, now, shared(provider, account)):
        return None   # the common case: nothing to do, no lock and no second reading
    with _locked(provider, account):
        if remeter:
            windows, reading = remeter()
            if not windows:
                return None
        return _consume_locked(db, cfg, provider, prov, windows, reading, account, now)


def _consume_locked(db, cfg, provider, prov, windows, reading, account, now) -> dict | None:
    entries = shared(provider, account)
    pend = next((e for e in entries if e.get("state") == "pending"), None)
    rest = [e for e in entries if e is not pend]

    def end(pick: dict, outcome: str, reason: str = "") -> dict:
        _write_shared(provider, account, rest + [{**pick, "state": "done", "outcome": outcome, "ts": now}], now)
        return _finish(db, provider, pick, outcome, now, reason)
    listed = bool(reading and reading.get("listed"))
    if pend and listed and pend.get("credit") not in {c["id"] for c in reading.get("resets") or []}:
        # Its credit left the list: the call went through before an answer was lost (or the
        # user consumed it). Either way that window had its reset.
        return end(pend, "reset (confirmed by the next reading)")
    want = decide(db, cfg, provider, windows, reading, now, rest)
    if pend:
        if not (want and abs(float(pend.get("against") or 0) - want["against"]) < SAME_WINDOW_S):
            if listed:   # the credit is still listed and nothing holds that window: the call never took effect
                _write_shared(provider, account, rest, now)
                pend = None
            else:
                return None   # no list to tell by: keep the key for the next reading
        elif now - float(pend.get("last_try") or 0) < RETRY_S:
            return None
    if pend:
        pick = pend
    elif want:
        pick = {**want, "key": uuid.uuid4().hex, "ts": now, "attempts": 0, "state": "pending"}
    else:
        return None
    pick = {**pick, "attempts": int(pick.get("attempts") or 0) + 1, "last_try": now}
    _write_shared(provider, account, rest + [pick], now)   # saved before the call: every retry reuses the key
    try:
        res = prov.consume_reset(pick["credit"], pick["key"])
    except Exception as e:
        res = {"transport": True, "error": f"{type(e).__name__}: {e}"[:300]}
    if res is None:   # the provider cannot consume resets
        _write_shared(provider, account, rest, now)
        return None
    kind, what = classify(res)
    what = what[:300]
    if kind == "success":
        return end(pick, what)
    if kind == "final":
        return end(pick, what, FINAL[what])
    if kind == "transport" and pick["attempts"] < MAX_TRIES:
        _write_shared(provider, account, rest + [{**pick, "error": what}], now)
        return {"error": what, "attempts": pick["attempts"]}
    return end(pick, f"failed: {what}")


def unreported(db: DB, provider: str) -> list[dict]:
    return [u for u in used(db, provider) if not u.get("reported")]


def mark_reported(db: DB, provider: str, done: list[dict]) -> None:
    """Mark the entries `done` reported (by credit and time: one added meanwhile stays unreported)."""
    keys = {(u.get("credit"), u.get("ts")) for u in done}
    with db.tx():
        db.set_kv(USED + provider, [{**u, "reported": True} if (u.get("credit"), u.get("ts")) in keys else u
                                    for u in used(db, provider)])


def report_text(provider: str, u: dict, fmt=None) -> str:
    fmt = fmt or (lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)))
    why = ("its window was full and would not reset for hours" if u.get("why") == "held"
           else "the credit was about to expire unused")
    if u.get("outcome") in FINAL:
        return (f"Did not use a free {provider} window reset ({why}): the provider answered {u['outcome']} "
                f"({u.get('reason') or FINAL[u['outcome']]}). Held work waits for the window's own reset.")
    if str(u.get("outcome") or "").startswith("failed"):
        return (f"Could not use a free {provider} window reset ({why}): {u['outcome']}. "
                f"Held work waits for the window's own reset.")
    exp = f", expiring {fmt(u['expires_at'])}" if u.get("expires_at") else ""
    return (f"Used one free {provider} window reset{exp}: the {u.get('window')} window was at "
            f"{float(u.get('utilization') or 0):.0f}% ({why}). Outcome: {u.get('outcome')}. Held work starts again.")


def summary(reading: dict | None) -> str:
    """'2 free resets', 'paid credits', both, or '' for the budget line."""
    if not reading:
        return ""
    parts = []
    n = reading.get("free_resets")
    if n and not reading.get("refused"):
        parts.append(f"{n} free reset{'s' if n != 1 else ''}")
    if reading.get("spend_control_reached") is True:
        parts.append("spend limit reached")
    elif paid_open(reading):
        parts.append("paid credits")
    return ", ".join(parts)


def unblock_path(db: DB, cfg: dict, provider: str, now: float | None = None) -> str:
    """For the blocker sweep: what can lift a provider's window hold, or ''."""
    now = now or time.time()
    r = latest(db, provider, now)
    bits = []
    if r and r.get("free_resets") and not r.get("refused"):
        auto = "consumed automatically when held" if settings(cfg)["reset_credits_auto"] else "auto-consume off"
        bits.append(f"{r['free_resets']} free window reset(s) ({auto})")
    if paid_open(r):
        bits.append("paid credits (the window does not hold; the dollar caps apply)")
    last = (used(db, provider) or [None])[-1]
    if last and now - float(last.get("ts") or 0) < 7 * 86400:
        bits.append(f"last free reset {(now - float(last['ts'])) / 3600:.1f} h ago: {last.get('outcome')}")
    return "; ".join(bits)
