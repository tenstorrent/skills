# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Plain replies: a coordinator `reply` or `notify` answers first, stays short and describes the
harness by what it does, not by its internal names ("the test queue runs 4 at a time", not "the
slot gate").

Model-free and flag-only: `check` runs after a batch is applied and never rejects anything. A text is
flagged when it uses more than `coordinator.reply_max_terms` (MAX_TERMS) internal terms with no plain
explanation, or when it is longer than `coordinator.reply_soft_chars` (SOFT_CHARS) and its first
sentence does not answer (a preamble such as "Here's the status:" or a heading). The term list is
TERMS, replaced by `coordinator.reply_terms`. A term counts as explained when a parenthesis or
"means"/"is the"/"i.e." follows it. Each checked text adds a record (its length, whether flagged);
the next digest's Course check carries one line naming the flagged ones (`digest_lines`), shown once,
and the daily review reports the median length and the flagged share (`audit_line`)."""
from __future__ import annotations

import re
import statistics
import time

from .db import DB

KEY = "plain_replies"   # kv: [{"msg", "ts", "kind", "chars", "why", "turn", "shown"}], newest last
KEEP_S = 7 * 86400
KEEP_N = 1000
DAY = 86400
TERMS = ("slot", "lane", "gate", "digest", "tick", "episode", "kv", "start_when", "retry_when", "precheck",
         "backoff", "probe", "fingerprint")
MAX_TERMS = 1      # more unexplained internal terms than this flags a text
SOFT_CHARS = 800   # longer than this flags a text whose first sentence does not answer
FIRST_MAX = 240    # a first sentence longer than this does not answer
PREAMBLE = re.compile(r"^\W*(?:here(?:'s| is| are)|let me|i'll|i will|i've (?:looked|checked)|i (?:looked|checked)|"
                      r"thanks|good question|great question|ok(?:ay)?,? so|so,|update:|status update|summary|"
                      r"quick update|a few things|some context|background)\b", re.I)
EXPLAIN = re.compile(r"^(?:e?s)?\W{0,3}(?:\(|means\b|is the\b|is a\b|i\.e\.|that is\b|=)", re.I)


def _cfg(cfg: dict | None) -> dict:
    return (cfg or {}).get("coordinator") or {}


def terms_of(cfg: dict | None) -> tuple[str, ...]:
    got = _cfg(cfg).get("reply_terms")
    return tuple(str(t).strip().lower() for t in got if str(t).strip()) if isinstance(got, list) else TERMS


def _num(cfg: dict | None, key: str, default: int) -> int:
    try:
        return max(0, int(_cfg(cfg).get(key, default)))
    except (TypeError, ValueError):
        return default


def jargon(text: str, terms: tuple[str, ...] = TERMS) -> list[str]:
    """The internal terms `text` uses with no plain explanation after them, one entry per use."""
    out = []
    for t in terms:
        for m in re.finditer(rf"(?<![\w-]){re.escape(t)}(?=(?:e?s)?(?![\w-]))", text or "", re.I):
            if not EXPLAIN.match(text[m.end():m.end() + 16]):
                out.append(t)
    return out


def first_sentence(text: str) -> str:
    for line in (text or "").strip().splitlines():
        if line.strip():
            return re.split(r"(?<=[.!?])\s", line.strip(), maxsplit=1)[0]
    return ""


def answers_first(text: str) -> bool:
    """Whether the first sentence reads as an answer or outcome, not a preamble, heading or list."""
    s = first_sentence(text).removeprefix("**")   # a bold lead ("**Done.** ...") still answers
    return bool(s) and len(s) <= FIRST_MAX and not s.endswith(":") and not s.startswith(("#", "-", "*", "|", "```")) \
        and not PREAMBLE.match(s)


def why(text: str, cfg: dict | None = None) -> str:
    """Why `text` is flagged ('' when it is plain)."""
    out = []
    if len(used := jargon(text, terms_of(cfg))) > _num(cfg, "reply_max_terms", MAX_TERMS):
        out.append("internal terms (" + ", ".join(sorted(set(used))) + ")")
    if len(text or "") > (soft := _num(cfg, "reply_soft_chars", SOFT_CHARS)) and not answers_first(text):
        out.append(f"{len(text):,} chars over {soft:,} with no answer first")
    return "; ".join(out)


def _records(db: DB) -> list[dict]:
    got = db.kv(KEY, []) or []
    return [r for r in got if isinstance(r, dict) and str(r.get("msg", "")).isdigit()]


def check(db: DB, sent: list[tuple[int, str, str]], cfg: dict | None = None, turn=None,
          now: float | None = None) -> list[int]:
    """After a batch: record each sent (message id, kind, text) and flag the ones `why` names. A
    coordinator turn (`turn` set) marks flags from earlier turns shown; a turn-less apply marks none.
    Returns the flagged message ids."""
    now = time.time() if now is None else now
    recs = _records(db)
    for r in recs:
        if turn is not None and r.get("turn") != turn:
            r["shown"] = True
    have = {int(r["msg"]) for r in recs}
    flagged = []
    for mid, kind, text in sent:
        if mid in have:
            continue   # the same turn applied again
        w = why(text, cfg)
        recs.append({"msg": mid, "ts": now, "kind": kind, "chars": len(text or ""), "why": w, "turn": turn,
                     "shown": not w})
        if w:
            flagged.append(mid)
    db.set_kv(KEY, [r for r in recs if now - float(r.get("ts") or 0) <= KEEP_S][-KEEP_N:])
    return flagged


def digest_lines(db: DB, now: float | None = None) -> list[str]:
    """One Course check line for the last turn's flagged replies; [] when there are none."""
    now = time.time() if now is None else now
    got = [r for r in _records(db) if r.get("why") and not r.get("shown") and now - float(r.get("ts") or 0) <= DAY]
    if not got:
        return []
    said = "; ".join(f"{r.get('kind') or 'reply'} #{r['msg']}: {r['why']}" for r in got[:3])
    more = f" and {len(got) - 3} more" if len(got) > 3 else ""
    return [f"- reply not plain: {said}{more} | answer in the first sentence, keep it short, and say what "
            "the harness does instead of its internal names"]


def audit_line(db: DB, since: float) -> str:
    """The daily review's line: the median reply length and the flagged share since `since`; '' with none."""
    got = [r for r in _records(db) if float(r.get("ts") or 0) >= since]
    if not got:
        return ""
    bad = sum(bool(r.get("why")) for r in got)
    med = statistics.median(int(r.get("chars") or 0) for r in got)
    return (f"replies and notifies to the user: {len(got)}, median {med:,.0f} chars, {bad} flagged "
            f"({100 * bad / len(got):.0f}%) as long without an answer first or heavy with internal terms")
