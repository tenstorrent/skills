# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Status claims checked against live state before a message reaches the user. Model-free.

detect() finds the sentences that claim a live state: something is running, started, stopped, raised,
lowered, set, written, landed, deployed, pushed, restarted or rebooted. A plan or condition ('I'll start
X', 'once it is running', 'it will be deployed'), a negation before the verb and a question are not
claims; neither is text in backticks.

check() judges each claim sentence, first backing that applies:
- a `claims` entry of the message whose text matches the sentence: its `expect` probe's exit code when
  the daemon ran it before the batch (prerun: read-only, PROBE_TIMEOUT_S each, never inside a database
  transaction): 0 holds, 1 (or any other answer but 75, 255 or a timeout) does not; one with a future
  `due`, a `task` or an `action` is tracked by obligations.record_claims and sent as written;
- a config key it names (`budget.daily_usd`, or an ALIASES phrase such as 'daily cap') with 'to <value>':
  the live config must hold that value;
- the tasks (#id) and runs ('run 123') it names as a claim's subject, each by the claim of its own clause
  (_bound; an id after the verb, but a start verb's target, or after a hedge word is left out, a binding
  that is ambiguous is unverified, never false): a task running for 'running'/'restarted', started for
  'started', not running for 'stopped', done for the rest; a run running, any run for 'started', ended for
  the rest. A worker notify's own task and run (relayed after its run ended) count as running then.
A claim that does not hold is not sent as written: it goes prefixed with what is true (FALSE_MARK), and
the caller wakes the coordinator with a high `claim_false` event. A claim nothing could check goes with
UNVERIFIED after it. Callers: coordinator.apply (replies and notifies, turn and session mode) and
daemon.relay_worker_notifies (worker `ttp notify`, record and config checks only). handoff_mark() marks a
worker hand-off that claims a live state and cites no evidence (an artifact or a command-output path) in
the digest and its hand-off event. Any fault leaves the text as written: a message is never lost to this."""
from __future__ import annotations

import hashlib
import re
import subprocess
import time

from . import onhost

KIND = "claim_false"
UNVERIFIED = "(unverified)"
FALSE_MARK = "Not so when checked"
STATUS_TAIL = "\nStatus now: "   # onhost.guard's appended status, never judged
PROBE_TIMEOUT_S = 10   # one claim probe before the batch is applied
PROBE_BUDGET_S = 30    # all of a batch's claim probes together
# Phrases that name a setting without its key.
ALIASES = {"daily cap": "budget.daily_usd", "daily budget": "budget.daily_usd", "daily spend cap": "budget.daily_usd"}

_RUN_V = r"running|restarted|rebooted"
_STOP_V = r"stopped|killed|disabled"
_VERB = re.compile(rf"(?i)\b(?P<v>{_RUN_V}|started|{_STOP_V}|raised|lowered|increased|written|landed|deployed|"
                   r"pushed|enabled|(?:(?<=\bis )|(?<=\bare )|(?<=\bwas )|(?<=\bwere )|(?<=\bnow )|(?<=\bbeen ))set|set(?= to\b))\b")
# Words before the verb (same clause) that make it a plan, condition or negation, not a claim.
_HEDGE = re.compile(r"(?i)(?:\b(?:will|shall|going to|plan(?:s|ned)? to|about to|want to|needs? to|to be|to get|"
                    r"not|never|no longer|once|until|when|after|if|before|unless|should|would|could|might|may|can|"
                    r"let me|i['’]ll|we['’]ll|it['’]ll|try(?:ing)? to|whether)\b|n['’]t\b)[^.!?;,\n]{0,60}$")
_CODE = re.compile(r"`[^`\n]*`")
_TASK = re.compile(r"(?i)(?<!msg )(?<!message )(?<!pr )(?<!issue )(?<!order )(?<!reply )(?<!messages )#(\d{1,9})\b")
_RUN = re.compile(r"(?i)\brun (\d{1,9})\b")
_KEY = re.compile(r"\b([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)\b")
_VALUE = re.compile(r"(?i)\bto\s+\$?([\w.:-]+)")
# Verbs that only bind an id to an ended state ('#1 finished'); not claims on their own.
_ENDED = re.compile(r"(?i)\b(?:finished|completed|merged|ended|done|passed)\b")
_FILLER = {"task", "tasks", "run", "runs", "the", "both", "also", "too", "then", "and", "but"}
# Words that may stand between a verb and the id it is about ('started on #1').
_ATTACH = _FILLER | {"on", "in", "for", "at"}
_CLAUSE = re.compile(r"(?i)[,;]|\s(?:and|but)\s")
_SPLIT = re.compile(r"((?<=[.!?;])[ \t]+|\n)")
_WORD = re.compile(r"[a-z0-9]{3,}")


def _verb(sentence: str) -> str | None:
    """The claim verb of `sentence`, or None: a plan, condition, negation, question or no status verb."""
    s = _CODE.sub(" ", sentence)
    if not s.strip() or s.rstrip().endswith("?") or onhost.MARK in s or s.lstrip().startswith(FALSE_MARK):
        return None
    for m in _verbs(s):
        return m["v"].lower()
    return None


def _verbs(s: str) -> list:
    """The unhedged claim verb matches of `s` (backticked code already blanked)."""
    return [m for m in _VERB.finditer(s) if not _HEDGE.search(s[:m.start()])]


def _bound(sentence: str) -> list[tuple[str, int, str | None]]:
    """Each task ('task') and run ('run') id of `sentence` that a claim is about, with the class of the
    claim verb after it in its own clause (split on ',', ';', ' and ', ' but '), or of the first verb after
    it when its clause holds ids alone ('#1, #2 and #3 landed'), or of a start verb before it in a clause with
    no id before that verb when only small words stand between ('runs started on #1'); None where its own
    clause holds several classes. Any other id after a verb or a hedge word in its clause (the verb's
    object, 'after #1 landed'), or whose clause verb is hedged, is not claimed and left out."""
    s = _CODE.sub(" ", sentence)
    verbs = [(m.start(), m.end(), _cls(m["v"].lower())) for m in _verbs(s)]
    verbs += [(m.start(), m.end(), "done") for m in _ENDED.finditer(s) if not _HEDGE.search(s[:m.start()])]
    verbs.sort()
    cuts = [0] + [m.end() for m in _CLAUSE.finditer(s)] + [len(s)]
    out = []
    for kind, rx in (("task", _TASK), ("run", _RUN)):
        for m in rx.finditer(s):
            lo = max(c for c in cuts if c <= m.start())
            hi = min(c for c in cuts if c > m.start())
            before, after = s[lo:m.start()], s[m.end():hi]
            if _HEDGE.search(before):
                continue   # 'after #1 landed', 'once #1 lands': a condition, not a claim about it
            if _VERB.search(before) or _ENDED.search(before):
                # Bound only as a start verb's subject-less target ('runs started on #1'); any other id
                # after a verb ('A review is running for #1') is its object: left out, never false.
                pre = [v for v in verbs if lo <= v[0] < m.start()]
                if not pre or pre[-1][2] != "start" or _TASK.search(s[lo:pre[-1][0]]) or _RUN.search(s[lo:pre[-1][0]]) \
                        or not set(re.findall(r"[a-z]+", s[pre[-1][1]:m.start()].lower())) <= _ATTACH:
                    continue
                out.append((kind, int(m.group(1)), pre[-1][2]))
                continue
            own = {c for at, _, c in verbs if m.end() <= at < hi}
            if not own and (_VERB.search(after) or _ENDED.search(after)):
                continue   # its clause's verb is a plan or a condition: no claim about it
            alone = set(re.findall(r"[a-z]+", _RUN.sub(" ", _TASK.sub(" ", s[lo:hi])).lower())) <= _FILLER
            nxt = [c for at, _, c in verbs if at > m.start()][:1] if alone else []
            cls = own or set(nxt)
            out.append((kind, int(m.group(1)), next(iter(cls)) if len(cls) == 1 else None))
    return out


def detect(text: str) -> list[str]:
    """The claim sentences of `text`."""
    return [s.strip() for s in _SPLIT.split(text or "")[::2] if _verb(s)]


def _cls(verb: str) -> str:
    return "run" if re.fullmatch(_RUN_V, verb) else "start" if verb == "started" else \
        "stop" if re.fullmatch(_STOP_V, verb) else "done"


def _entry(sentence: str, claims: list) -> dict | None:
    s = set(_WORD.findall(sentence.lower()))
    for c in claims or []:
        w = set(_WORD.findall(str((c or {}).get("text") or "").lower()))
        if w and len(w & s) * 2 >= len(w):
            return c
    return None


def _config_value(cfg: dict, key: str):
    cur = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return KeyError
        cur = cur[part]
    return cur


def _same(live, said: str) -> bool:
    try:
        return float(live) == float(said.rstrip(".").replace(",", ""))
    except (TypeError, ValueError):
        return str(live).strip().lower() == said.rstrip(".").lower()


def _judge(p, sentence: str, verb: str, claims: list, now: float, own: dict | None = None) -> tuple[str, str]:
    """('ok' | 'false' | 'unverified', what is true or why)."""
    cls = _cls(verb)
    if c := _entry(sentence, claims):
        if c.get("task") not in (None, "") or c.get("action") not in (None, ""):
            return "ok", "tracked by its task"
        if c.get("due") not in (None, ""):
            from .obligations import _when
            if _when(c["due"], "claim due") > now:
                return "ok", "tracked until its due"
        rc = c.get("_rc")
        if rc == 0:
            return "ok", "its check exited 0"
        if rc not in (None, 75, 255, "timeout", "could not start"):
            return "false", f"its check `{str(c.get('expect'))[:120]}` exited {rc}"
    low = sentence.lower()
    keys = [k for k in _KEY.findall(sentence)] + [v for a, v in ALIASES.items() if a in low]
    cfg = p.config()
    for key in keys:
        live = _config_value(cfg, key)
        if live is KeyError:
            continue
        if (m := _VALUE.search(sentence[sentence.find(key) if key in sentence else 0:])) and cls == "done":
            return ("ok", f"{key} is {live}") if _same(live, m.group(1)) else ("false", f"{key} is {live}")
    own = own or {}
    bad, good, open_ = [], 0, 0
    for kind, i, c in _bound(sentence):
        if kind == "task":
            t = p.db.task(i)
            if not t:
                continue
            st = t["status"]
            if i == own.get("task"):   # the writer's own task, judged after its run ended: it ran when it wrote
                good, open_ = (good + 1, open_) if c in ("run", "start") else (good, open_ + 1)
                continue
            ran = st == "running" or bool(p.db.one("SELECT id FROM runs WHERE task=? LIMIT 1", (i,)))
            ok = {"run": st == "running", "start": ran, "stop": st != "running", "done": st == "done"}.get(c)
            state = f"#{i} is {st}"
        else:
            r = p.db.one("SELECT status FROM runs WHERE id=?", (i,))
            if not r:
                continue
            if i == own.get("run"):
                good, open_ = (good + 1, open_) if c in ("run", "start") else (good, open_ + 1)
                continue
            # A run that exists has started; 'running' and 'restarted' need it running, the rest ended.
            ok = {"run": r["status"] == "running", "start": True}.get(c, r["status"] != "running") if c else None
            state = f"run {i} is {r['status']}"
        if ok is None:
            open_ += 1   # which claim it is bound to is ambiguous: never judged false
        elif ok:
            good += 1
        else:
            bad.append(state)
    if bad:
        return "false", "; ".join(bad)
    if good and not open_:
        return "ok", "task and run records"
    return "unverified", "nothing could check it"


def _mark(sentence: str) -> str:
    m = re.search(r"[.!;:]+\s*$", sentence)
    return (sentence[:m.start()] + f" {UNVERIFIED}" + sentence[m.start():]) if m else f"{sentence} {UNVERIFIED}"


def check(p, text: str, claims: list | None = None, now: float | None = None,
          own: dict | None = None) -> tuple[str, list[tuple[str, str]], int]:
    """(text to send, [(false sentence, what is true)], count marked unverified). `own` ({'task': id, 'run': id}):
    the writer's own task and run, whose state at the end of its run is not what its notify claimed."""
    now = time.time() if now is None else now
    try:
        head, cut, tail = (text or "").partition(STATUS_TAIL)   # onhost's honest status lines: the harness's own
        parts = _SPLIT.split(head)
        false, unverified = [], 0
        for k in range(0, len(parts), 2):
            verb = _verb(parts[k])
            if not verb:
                continue
            got, why = _judge(p, parts[k], verb, claims or [], now, own)
            lead = parts[k][:len(parts[k]) - len(parts[k].lstrip())]
            if got == "false":
                false.append((parts[k].strip(), why))
                parts[k] = f"{lead}{FALSE_MARK} ({why}): {parts[k].strip()}"
            elif got == "unverified":
                unverified += 1
                parts[k] = _mark(parts[k].rstrip()) + parts[k][len(parts[k].rstrip()):]
        return "".join(parts) + cut + tail, false, unverified
    except Exception:
        return text, [], 0


def raise_false(db, false: list, where: str, now: float | None = None) -> None:
    """Wake the coordinator with the claims of message `where` that did not hold (one high event)."""
    if not false:
        return
    lines = "; ".join(f"'{s[:200]}' ({why})" for s, why in false)
    fp = f"{KIND}:{where}:{hashlib.sha256(lines.encode()).hexdigest()[:12]}"
    if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return   # the same batch or run end applied again
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
         (time.time() if now is None else now, "daemon", KIND, fp, "high",
          f"{where} claimed what did not hold when checked before sending: {lines}. It was sent marked with what "
          f"is true. Tell that chat what is true now and fix or re-order the work; check live state before "
          f"you claim one"[:4000], "queued"))


def prerun(p, actions: list, env: dict | None = None) -> None:
    """Run the `expect` probes of the batch's reply and notify claims now, before the batch is applied, and
    keep each exit code as the claim's `_rc` (check reads it). Skipped inside a database transaction: a
    probe never holds the write lock. Bounded: PROBE_TIMEOUT_S each, PROBE_BUDGET_S in all."""
    if p.db.conn.in_transaction:
        return
    from .landed import probe_command
    from .probespawn import hidden
    end = time.monotonic() + PROBE_BUDGET_S
    for a in actions or []:
        if not isinstance(a, dict) or a.get("type") not in ("reply", "notify") or not isinstance(a.get("claims"), list):
            continue
        for c in a["claims"]:
            if not isinstance(c, dict) or not str(c.get("expect") or "").strip():
                continue
            left = end - time.monotonic()
            if left <= 1:
                return
            try:
                cmd, run_env = hidden(probe_command(str(c["expect"]), p), env)
                c["_rc"] = subprocess.run(cmd, shell=True, cwd=str(p.root), env=run_env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          timeout=min(PROBE_TIMEOUT_S, left)).returncode
            except subprocess.TimeoutExpired:
                c["_rc"] = "timeout"
            except OSError:
                c["_rc"] = "could not start"


_EVIDENCE = re.compile(r"(?:^|[\s(\"'`])(?:/|~/|\./|\$\w+/)\S+|\b\S+\.(?:log|out|txt|json|jsonl|rc|csv)\b")


def handoff_mark(result: dict) -> str:
    """' (unverified: ...)' for a hand-off whose summary claims a live state and cites no evidence."""
    try:
        res = result if isinstance(result, dict) else {}
        summary = str(res.get("summary") or "")
        if not detect(summary) or res.get("artifacts") or _EVIDENCE.search(summary):
            return ""
        return f" {UNVERIFIED[:-1]}: claims a live state, cites no command output)"
    except Exception:
        return ""
