# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Urgent messages never go silent: model-free intake, ack and a late-reply watchdog.

An inbound message (a chat message, `ttp say`, `ttp note --to`) is urgent when its sender marks it (an
`urgent:` or `[urgent]` prefix, `ttp say --urgent`) or a conservative classifier sees an urgent marker
word or a report that a person's live service is broken (classify; the `urgent_msg` settings add markers
or turn it off). For each one the daemon, with no model call:

1. posts an ack in the sender's chat within `ack_s` (60 s): who is on it (the coordinator, plus the task
   a `#<id>` in the text names, when open) and when the first finding is due (receipt + `first_s`,
   5 min), in the project's home zone, ending with the project's AI tag;
2. keeps the deadlines on disk (kv `urgent_msgs`), so a restart loses none and each fires once;
3. closes a record at the coordinator's first substantive reply in that chat after receipt (kind reply
   or ask: not the ack, a late notice or a digest line);
4. with still no reply `hold_s` (4.5 min) after receipt, posts a holding note in the sender's chat (marked
   as not a finding: when it was received, that the coordinator is on it, the next check), so the chat
   hears back within 5 minutes even when the turn is still thinking;
5. at a missed first-finding deadline raises one high `urgent_msg_late` event whose must-act item wakes
   the coordinator at once past the batching holds (or, with a turn running, reaches that turn as a steer
   update), and at a second miss (`second_s` later) notifies the user at high.

A turn that has the message in its prompt is told first, before its digest (`lead`), to post what it
knows with `ttp urgent-reply` before it reads anything; one still silent `nudge_s` (2.5 min) after
receipt gets a steer update saying so, which its hook hands over at its next tool call (`_nudge`).

A classifier false positive costs one ack and, if no reply follows, a holding note: nothing else changes.
The coordinator's turn takes an urgent message ahead of the relay window, debounce and queue (`first`),
and a turn already running gets it mid-run (`reach`): as a steer update where the provider's hook hands
updates over (read within `read_s`, 20 s, else the turn is cut; read but with no first finding posted
(`ttp urgent-reply`) within `answer_s`, 120 s, it is cut too), else the turn is cut after `cut_after_s`
(20 s) and the next one, which starts past the batching holds, reads the urgent message first. With no
turn running (a cut turn ended, a failed one, none yet) the daemon starts that next turn in the same
pass, past the coordinator's failure backoff (`next_turn_due`), not at the end of a full daemon pass.
Every provider ends with the message in a turn within seconds."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import coordroute, timefmt   # coordroute: a lane or shadow turn reads no messages

KV = "urgent_msgs"             # {message id: record}: the deadlines, kept on disk
CURSOR = "urgent_msg_cursor"   # the last inbound message id looked at
ACK_KIND, LATE_KIND = "urgent_ack", "urgent_late"
EVENT_KIND = "urgent_msg_late"
REPLY_KINDS = ("reply", "ask")   # an outbound message of these kinds answers the sender
DEFAULTS = {"enabled": True, "ack_s": 60, "first_s": 300, "second_s": 300, "markers": [], "keep_h": 24,
            "stale_s": 900, "cut_after_s": 20, "read_s": 20, "answer_s": 120, "nudge_s": 150, "hold_s": 270}
DONE_STATES = ("done", "failed", "cancelled")

_MARK_RE = re.compile(r"^\s*(?:\[urgent\]|urgent\s*[:!\-]|\*\*urgent\*\*)", re.I)
_WORD_RE = re.compile(r"\b(?:urgent(?:ly)?|asap|emergency|sev\s?[01]|p0|outage|drop everything|right now|"
                      r"immediately)\b", re.I)
_CALM_RE = re.compile(r"\b(?:not|isn'?t|non[- ]?|no)\s*(?:that\s+|very\s+)?urgent\b|\bno rush\b|\bwhenever\b", re.I)
_BROKEN_RE = re.compile(r"\b(?:down(?!-)|broken|dead|not working|doesn'?t work|stopped|offline|failing|failed|crash(?:ed|ing)?|"
                        r"frozen|stuck|black ?screen|no (?:video|audio|picture|signal)|unreachable|can'?t (?:see|watch|connect))\b",
                        re.I)
_LIVE_RE = re.compile(r"\b(?:live|stream(?:ing)?|production|prod|serving|customers?|users?|viewers?|on air)\b", re.I)
NEAR = 90   # characters between the broken word and the live-service word


def config(cfg: dict | None) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("urgent_msg") or {})}


def classify(cfg: dict | None, text: str, context: str = "") -> tuple[bool, str]:
    """(urgent, why) for one inbound message: sender's mark, an urgent marker word, or a broken live
    service. The relayer's `context` is read for the last two, never for the sender's mark."""
    c = config(cfg)
    if not c.get("enabled", True):
        return False, ""
    text = str(text or "")
    if _MARK_RE.search(text) or _MARK_RE.search(str(context or "")):
        return True, "marked urgent by the sender"
    body = f"{text}\n{context or ''}"
    broken = _live_service_broken(body)
    if broken:
        return True, broken
    if _CALM_RE.search(body):
        return False, ""
    extra = [str(m).strip() for m in c.get("markers") or [] if str(m).strip()]
    if (m := _WORD_RE.search(body)):
        return True, f"urgent marker '{m.group(0).lower()}'"
    low = body.lower()
    for m in extra:
        if m.lower() in low:
            return True, f"urgent marker '{m}'"
    return False, ""


def _live_service_broken(body: str) -> str:
    live = [m.start() for m in _LIVE_RE.finditer(body)]
    if not live:
        return ""
    for b in _BROKEN_RE.finditer(body):
        if any(abs(b.start() - i) <= NEAR for i in live):
            return f"report that a live service is broken ('{b.group(0).lower()}')"
    return ""


def mark(text: str, context: str = "") -> tuple[str, str]:
    """`text` (or, with none, `context`) marked urgent for the sender: an `urgent:` prefix."""
    if text.strip():
        return (text if _MARK_RE.search(text) else f"urgent: {text.strip()}"), context
    return text, (context if _MARK_RE.search(context) else f"urgent: {context.strip()}")


# records ------------------------------------------------------------------------------------------
def records(db) -> dict:
    v = db.kv(KV, {})
    return v if isinstance(v, dict) else {}


def _owners(db, text: str) -> list[int]:
    ids = sorted({int(i) for i in re.findall(r"(?<![\w&])#(\d{1,7})\b", text or "")})
    if not ids:
        return []
    rows = db.q(f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)
    return [r["id"] for r in rows if r["status"] not in DONE_STATES]


def _answered(db, rec: dict) -> bool:
    """The coordinator's first substantive reply in the record's chat since the message arrived."""
    chat = rec.get("chat")
    q = ",".join("?" * len(REPLY_KINDS))
    row = db.one(f"SELECT id FROM messages WHERE direction='out' AND id>? AND kind IN ({q}) AND "
                 f"(chat IS ? OR chat=?) LIMIT 1", (rec["mid"], *REPLY_KINDS, chat, chat or ""))
    return bool(row)


def ack_text(p, rec: dict) -> str:
    who = "The coordinator is on it" + (
        f" (task #{', #'.join(map(str, rec['tasks']))} " + ("owns" if len(rec["tasks"]) == 1 else "own") + " it)"
        if rec.get("tasks") else "")
    return _tagged(p.db, f"Got your urgent message ({timefmt.short(rec['ts'], p, rec['ts'])}). {who}. First finding due by "
            f"{timefmt.short(rec['find_due'], p, rec['ts'])}.")


def _tagged(db, text: str) -> str:
    tag = getattr(db, "ai_tag", "") or ""
    return f"{text} {tag}" if tag else text


def scan(p, cfg: dict | None, now: float) -> list[dict]:
    """Classify the inbound messages since the cursor; a record and the ack for each urgent one, at most
    `stale_s` old (an older one is a restart's backlog: the turn reads it, nobody waits on an ack)."""
    db, c, new = p.db, config(cfg), []
    if not c.get("enabled", True):
        return new
    cur = int(db.kv(CURSOR, 0) or 0)
    rows = db.q("SELECT * FROM messages WHERE direction='in' AND id>? ORDER BY id LIMIT 200", (cur,))
    if not rows:
        return new
    recs = records(db)
    for m in rows:
        cur = m["id"]
        if now - float(m["ts"]) > float(c["stale_s"]) or str(m["id"]) in recs:
            continue
        urgent, why = classify(cfg, m["text"], m["context"] or "")
        if not urgent:
            continue
        ts = float(m["ts"])
        rec = {"mid": m["id"], "chat": m["chat"], "ts": ts, "why": why, "tasks": _owners(db, m["text"] or ""),
               "ack_due": ts + float(c["ack_s"]), "find_due": ts + float(c["first_s"]),
               "second_due": ts + float(c["first_s"]) + float(c["second_s"]),
               "acked": None, "late": None, "escalated": None, "closed": None, "reached": None,
               "nudged": None, "held": None}
        recs[str(m["id"])] = rec
        new.append(rec)
    db.set_kv(CURSOR, cur)
    if new:
        db.set_kv(KV, recs)
    return new


def tick(p, cfg: dict | None, now: float | None = None, steer=None) -> list[str]:
    """One daemon pass: scan, ack, watch the deadlines. `steer(text, key)` delivers a late item into a
    running coordinator turn (True when one took it). Returns what happened, for the log."""
    now = time.time() if now is None else now
    db, c, out = p.db, config(cfg), []
    scan(p, cfg, now)
    recs = records(db)
    if not recs:
        return out
    changed = False
    for key, rec in list(recs.items()):
        if rec.get("closed"):
            if now - float(rec["closed"]) > float(c["keep_h"]) * 3600:
                del recs[key]
                changed = True
            continue
        if _answered(db, rec):
            rec["closed"] = now
            changed = True
            if rec.get("acked") is None:
                rec["acked"] = now   # answered before the ack: the answer is the ack
            continue
        if rec.get("acked") is None:
            db.post("out", ack_text(p, rec), chat=rec["chat"], kind="urgent_ack", severity="normal", keep_tag=True)
            rec["acked"] = now
            changed = True
            out.append(f"ack {key}")
        # A turn that had it and ended with no reply (cut, failed, lost): reached again, so the next turn
        # is started for it at once (next_turn_due).
        if rec.get("reached") in (None, "steer") or rec.get("reached") in ("in_turn", "cut") and not _running(db):
            how = reach(p, rec, c, now)
            if how != rec.get("reached"):
                rec["reached"] = how
                changed = True
                if how:
                    out.append(f"reach {key} {how}")
        if rec.get("nudged") is None and now >= rec["ts"] + float(c["nudge_s"]) and _nudge(p, rec, now, steer):
            rec["nudged"] = now
            changed = True
            out.append(f"nudge {key}")
        if rec.get("held") is None and rec.get("late") is None and now >= rec["ts"] + float(c["hold_s"]):
            rec["held"] = now
            changed = True
            out.append(f"hold {key}")
            db.post("out", _tagged(db, f"Holding note, not a finding: your urgent message from "
                                       f"{timefmt.short(rec['ts'], p, now)} is with the coordinator, which has not "
                                       f"posted its first finding yet. Next check {timefmt.short(rec['find_due'], p, now)}."),
                    chat=rec["chat"], kind=LATE_KIND, severity="high", keep_tag=True)
        if rec.get("late") is None and now >= rec["find_due"]:
            rec["late"] = now
            changed = True
            out.append(f"late {key}")
            _late(p, db, rec, c, now, steer)
        if rec.get("late") is not None and rec.get("escalated") is None and now >= rec["second_due"]:
            rec["escalated"] = now
            changed = True
            out.append(f"escalated {key}")
            db.post("out", f"Urgent message #{rec['mid']} in chat {rec['chat'] or 'all'} still has no reply after "
                      f"{int((now - rec['ts']) // 60)} min (received {timefmt.short(rec['ts'], p, now)}). The "
                      f"coordinator was woken for it at {timefmt.short(rec['late'], p, now)}.",
                  chat=None, kind="alert", severity="high")
    if changed:
        db.set_kv(KV, recs)
    return out


def _late(p, db, rec: dict, c: dict, now: float, steer) -> None:
    text = (f"Urgent message #{rec['mid']} in chat {rec['chat'] or 'all'} got no finding by "
            f"{timefmt.short(rec['find_due'], p, now)} ({rec['why']}). Post the first finding in that chat now "
            f"(`ttp urgent-reply`, or a reply action), with times.")
    it = {"key": f"urgent#{rec['mid']}", "chat": rec["chat"] or "all", "tasks": rec.get("tasks") or [],
          "kind": EVENT_KIND}
    from . import obligations
    obligations._raise(db, now, f"urgent#{rec['mid']}:late", EVENT_KIND, text, None, {"must_act": it})
    if steer is not None:
        try:
            steer(text, f"urgent-late-{rec['mid']}")
        except Exception:   # the event still wakes the next turn
            pass


def _nudge(p, rec: dict, now: float, steer) -> bool:
    """A running turn that has the urgent message in its prompt but posted nothing yet: one steer update
    telling it to post what it knows now (its hook hands it over at the next tool call). True once sent."""
    if steer is None or rec.get("reached") == "steer":   # a steered turn was already told to post first
        return False
    run = _running(p.db)
    row = p.db.one("SELECT handled FROM messages WHERE id=?", (rec["mid"],))
    if not run or not (_in_turn(run, rec["mid"]) or (row and row["handled"])):
        return False
    try:
        return bool(steer(f"Urgent message #{rec['mid']} in chat {rec['chat'] or 'all'} (received "
                          f"{timefmt.short(rec['ts'], p, now)}) still has no finding; due "
                          f"{timefmt.short(rec['find_due'], p, now)}. Post what you know now: `ttp urgent-reply "
                          f"{rec['chat'] or 'all'} \"<finding so far, with times>\"`, then keep going.",
                          f"urgent-nudge-{rec['mid']}"))
    except Exception:   # the holding note still reaches the chat
        return False


def lead(p, ids) -> str:
    """The wake prompt's first lines for the open urgent messages among `ids`: post first, then read."""
    recs = records(p.db)
    rows = [r for i in sorted(ids or ()) if (r := recs.get(str(i))) and not r.get("closed")]
    if not rows:
        return ""
    now = time.time()
    return ("## Urgent: post first\nBefore you read or run anything else, post what you know now in each chat "
            "below (one line, with times), then investigate:\n" + "\n".join(
                f"- message #{r['mid']}, received {timefmt.short(r['ts'], p, now)}, first finding due "
                f"{timefmt.short(r['find_due'], p, now)}: `ttp urgent-reply {r['chat'] or 'all'} \"<what you know "
                f"now, with times>\"`" for r in rows))


# the turn ---------------------------------------------------------------------------------------
def open_for_chat(db, chat) -> list[dict]:
    """Records still awaiting a reply in `chat` (None: any), for `ttp urgent-reply` and the turn."""
    return [r for r in records(db).values() if not r.get("closed") and (chat is None or r.get("chat") == chat)]


def pending(db) -> list[dict]:
    return open_for_chat(db, None)


def deliver_running(p, text: str, key: str) -> bool:
    """Append `text` to the steer.md of a running coordinator turn (its hook hands it over after the next
    tool call); True when a turn is running to take it."""
    from .coordinator import _append_update
    r = p.db.one("SELECT dir FROM runs WHERE role='coordinator' AND status='running' AND " + coordroute.OWN_SQL + " ORDER BY id DESC LIMIT 1")
    if not r or not r["dir"]:
        return False
    _append_update(Path(r["dir"]) / "steer.md", text, key)
    return True


# the turn: urgent first, and mid-run ---------------------------------------------------------------
def first(db, cfg: dict | None, msgs: list) -> tuple[list, set]:
    """`msgs` (unhandled inbound rows) with the urgent ones first, in their own order, and the set of
    their ids: a record already open for it, or the classifier (the same conservative one the ack uses)."""
    c = config(cfg)
    if not c.get("enabled", True) or not msgs:
        return list(msgs), set()
    recs = records(db)
    ids = set()
    for m in msgs:
        rec = recs.get(str(m["id"]))
        if (rec and not rec.get("closed")) or (not rec and classify(cfg, m["text"], m["context"] or "")[0]):
            ids.add(m["id"])
    return sorted(msgs, key=lambda m: m["id"] not in ids), ids


def next_turn_due(db, cfg: dict | None) -> bool:
    """An open urgent message no turn has yet and no coordinator turn running: the daemon starts the next
    turn now, in the same pass and past its failure backoff, not at the end of a full daemon pass. A paused
    project starts none."""
    if not config(cfg).get("enabled", True) or db.kv("paused", False) or _running(db):
        return False
    for rec in pending(db):
        row = db.one("SELECT handled FROM messages WHERE id=?", (rec["mid"],))
        if row and not row["handled"]:
            return True
    return False


def _running(db):
    return db.one("SELECT id, dir, provider, started, note FROM runs WHERE role='coordinator' AND "
                  "status='running' AND " + coordroute.OWN_SQL + " ORDER BY id DESC LIMIT 1")


def _in_turn(run, mid: int) -> bool:
    try:
        return mid in ((json.loads(run["note"] or "{}") or {}).get("messages") or [])
    except (TypeError, ValueError, AttributeError):
        return False


def reach(p, rec: dict, c: dict, now: float) -> str | None:
    """Get the urgent message to the coordinator now. Returns where it stands, kept in the record:
    "next" (no turn runs: the next one starts past the holds), "in_turn" (the running turn already has it
    in its prompt), "steer" (an update is in the running turn's steer.md, read or not, and no first finding
    yet: watched until `answer_s`), "cut" (the
    turn was stopped to restart with it first: its messages and events stay queued; the session resumes),
    None (a turn that cannot read updates is younger than `cut_after_s`: tried again next pass)."""
    db = p.db
    run = _running(db)
    if not run or not run["dir"]:
        return "next"
    row = db.one("SELECT handled FROM messages WHERE id=?", (rec["mid"],))
    if _in_turn(run, rec["mid"]) or (row and row["handled"]):   # a turn read it already
        return "in_turn"
    run_dir = Path(run["dir"])
    if (run_dir / "STOP").exists():
        return "next"   # already ending: the next turn reads it
    from .hook import steer_hooked
    if steer_hooked(run_dir, run["provider"]):
        if rec.get("reached") != "steer":
            m = db.one("SELECT text, context FROM messages WHERE id=?", (rec["mid"],))
            from .coordinator import _append_update
            _append_update(run_dir / "steer.md", (
                f"URGENT message #{rec['mid']} in chat {rec['chat'] or 'all'} arrived while you work ({rec['why']}): "
                f"{(m['text'] if m else '')[:600]}\nPost the first finding in that chat now, with times "
                f"(`ttp urgent-reply`), before anything else."), f"urgent-{rec['mid']}")
            rec["steered"] = now
            return "steer"
        from .hook import unread_update
        waited = now - float(rec.get("steered") or now)
        if unread_update(run_dir)[0]:   # no tool call to carry the update (a long reasoning step or a
            # compaction): restart with it first
            return "steer" if waited < float(c["read_s"]) else _cut(run_dir)
        # Read: the record stays "steer" so each pass checks that the first finding follows; tick closes
        # it at the reply. Read with none posted within answer_s: the turn is cut and the next one,
        # started at once, has it first.
        return "steer" if waited < float(c.get("answer_s", DEFAULTS["answer_s"])) else _cut(run_dir)
    if now - float(run["started"] or now) < float(c["cut_after_s"]):
        return None
    return _cut(run_dir)


def _cut(run_dir: Path) -> str:
    from . import runner
    runner.request_stop(run_dir, "preempt")   # a resumable stop: nothing is marked failed, the batch stays queued
    return "cut"
