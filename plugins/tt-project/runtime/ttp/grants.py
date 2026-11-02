# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Charter grants against the delivery settings: what the charter grants is what the config does.

A charter whose Policies (or verbatim Brief) grant something the live delivery settings do not
reflect stalls work behind an ask the user already answered: reviewed pushes wait for days on
"may I merge?" while the charter lists the repo for auto-merge. `reconcile` compares, model-free:

- auto-merge: the Policies line "Auto-merge repositories: <owner/repo, ...>" against
  delivery.auto_merge_repos, report-only: the coordinator can write that line itself, and merging
  is an irreversible permission only the user grants, so the daemon never writes the setting (the
  coordinator sets it with config_set in the turn carrying the user's word);
- push permission: a grant of pushes to the push branch (settinghold.charter_says_no_approval)
  against delivery.push_allowed;
- protected branch: the same grant, for a push branch only delivery.allow_protected_push_branch lets
  `ttp push` reach (main, master, the remote's default), against that setting.

The live config is read as it is on disk (uncommitted edits included). A grant the config lacks is
applied only on the user's own word on record: the Brief, or a Policies entry that CHARTER.history.md
logs as added in a turn carrying the user's message (checked in the database: inbound, kind user,
not posted by the harness); auto-merge never. Then the daemon sets it, says so in one low-severity
alert, and closes open asks naming the setting, instead of asking again. Nothing the charter does
not grant is ever set; the config only gains what the grant names.

A mismatch it cannot apply (any auto-merge mismatch, no user word on record, a charter sentence
forbidding it, a setting set back after the daemon applied it, a failed write) is a harness defect:
one keyed event (EVENT, fingerprint per setting and state) for the coordinator, never an ask. One
exception keeps a single question: a protected-branch grant with no user word on record is
settinghold's setup ask, which this leaves to it.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import configwho, settinghold

EVENT = "grant_mismatch"
APPLIED = "grant_applied"
AUTO_MERGE = "delivery.auto_merge_repos"
PUSH = "delivery.push_allowed"
PROTECTED = "delivery.allow_protected_push_branch"
# What the daemon applied, per setting and charter line ({key: {line: [items]}}): an item it applied and
# the config lacks later was set back, and is never applied again. Push settings only.
APPLIED_KV = "grants_applied"
# One physical line: [ \t], never \s, so a bare "Auto-merge repositories:" cannot read the next paragraph.
_AUTO_MERGE_LINE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?\**auto-merge[ \t]+repositor(?:y|ies)\**[ \t]*:[ \t]*(?P<v>[^\n]*)$",
                              re.I | re.M)
_REPO_TOKEN = r"`?[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w-]*(?:\.[\w-]+)*`?"
# A plain list, "owner/repo[, owner/repo ...]" (", and" or "and" before the last), with an optional
# trailing period or parenthetical note: the only shape that grants anything.
_REPO_LIST = re.compile(rf"^(?P<list>{_REPO_TOKEN}(?:[ \t]*,[ \t]*(?:and[ \t]+)?{_REPO_TOKEN}|[ \t]+and[ \t]+{_REPO_TOKEN})*)"
                        r"[ \t]*\.?[ \t]*(?:\([^()\n]*\))?[ \t]*\.?[ \t]*$")
_REPO = re.compile(_REPO_TOKEN)
_NONE = re.compile(r"^\(?(?:(?:none|nothing|no)\b|-(?![\w/]))", re.I)
_NEGATED = re.compile(r"\b(?:never|not|except|no|without|nor)\b|n't\b", re.I)
_HIST = re.compile(r"^### (?:Added to|Replaced in) (?P<sec>[^\n]+?) \((?P<meta>[^)\n]*)\)\n(?P<body>.*?)(?=^### |\Z)",
                   re.M | re.S)
# A plain prohibition of pushing; "never force-push" forbids only rewriting the branch, not pushing to it.
_FORBIDS = re.compile(r"\b(?:never|do not|don't|must not|may not|no)\s+push", re.I)
_USER_MSG = re.compile(r"user message (#\d+(?:, #\d+)*)")


def _flat(s: str) -> str:
    return " ".join(str(s or "").split())


def sections(charter: str) -> dict[str, str]:
    """The Brief and Policies text of the charter ("brief", "policies"; dated Policies sections join
    the base one)."""
    from .prompts import charter_sections
    out = {"brief": "", "policies": ""}
    for head, body in charter_sections(charter or ""):
        name = head[3:].strip().lower()
        for k in out:
            if name.startswith(k[:5]):
                out[k] += "\n".join(body) + "\n"
    return out


def auto_merge_repos(policies: str) -> tuple[list[str] | None, list[str]]:
    """The repositories the Policies lines "Auto-merge repositories: ..." list ([] when each says none)
    and the lines that list them (each must be the user's word), or (None, lines) when a line is
    neither a plain list nor none and no line lists one, or (None, []) when there is no such line. Only
    a plain list grants: a value starting with none grants nothing whatever follows it, and one holding
    never, not, except, no or without is not read as a grant (a line it cannot read cleanly is a
    defect, never a grant). A template "none" left beside a line listing repos does not take them away."""
    hits = list(_AUTO_MERGE_LINE.finditer(policies or ""))
    if not hits:
        return None, []
    repos, granting, unclear = set(), [], []
    for m in hits:
        line, v = _flat(m.group(0)).lstrip("-* "), m.group("v").strip()
        if _NONE.match(v):
            continue
        plain = None if _NEGATED.search(v) else _REPO_LIST.match(v)
        if plain:
            repos |= {r.strip("`") for r in _REPO.findall(plain.group("list"))}
            granting.append(line)
        else:
            unclear.append(line)
    if repos:
        return sorted(repos), granting
    return (None, unclear) if unclear else ([], [])


def user_word(db, charter: str, history: str, line: str) -> str:
    """Where the user's own word on record grants `line`: "the Brief" when the verbatim Brief holds it,
    "user message #N" when CHARTER.history.md logs a Policies entry holding it as added in a turn
    carrying the user's message (inbound, kind user, not the harness's own), else ""."""
    want = _flat(line).lower()
    if not want:
        return ""
    if want in _flat(sections(charter)["brief"]).lower():
        return "the Brief"
    for m in _HIST.finditer(history or ""):
        if not m.group("sec").strip().lower().startswith("polic"):
            continue
        body = m.group("body")
        body = body.split("\nNow: ", 1)[1] if "\nNow: " in body else body
        ids = _USER_MSG.search(m.group("meta"))
        if not ids or want not in _flat(body).lower():
            continue
        nums = [int(x) for x in re.findall(r"\d+", ids.group(1))]
        rows = db.q(f"SELECT id, provenance FROM messages WHERE id IN ({','.join('?' * len(nums))}) "
                    f"AND direction='in' AND kind='user'", nums)
        own = [r["id"] for r in rows if (r["provenance"] or "") != "system"]
        if own:
            return "user message #" + ", #".join(map(str, sorted(own)))
    return ""


def forbidden(charter: str, push_branch: str) -> str:
    """A charter sentence forbidding pushes to the push branch (any section), or ""."""
    names = {n for n in (push_branch, push_branch.partition("/")[2]) if n}
    if not names:
        return ""
    target = re.compile(r"\bpush[ \t]+branch\b|(?<![\w/-])(?:" + "|".join(
        re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w/-])", re.I)
    for s in re.split(r"(?<=[.!?])\s+|\n+", charter or ""):
        if _FORBIDS.search(s) and target.search(s) and not settinghold._NO_APPROVAL.search(s) \
                and not settinghold._ONLY_IF.search(s):
            return _flat(s)
    return ""


def _push_grant(charter: str, branch: str) -> str:
    """A grant of pushes to the push branch in the Brief or the Policies, or ""."""
    parts = sections(charter)
    return settinghold.charter_says_no_approval(parts["policies"], branch) \
        or settinghold.charter_says_no_approval(parts["brief"], branch)


def checks(cfg: dict, charter: str, repo: Path | None = None) -> list[dict]:
    """Each delivery setting the charter grants and the config does not reflect: {key, want, have,
    line, lines, problem}: `lines` are the charter lines granting it (each needs the user's word),
    `problem` is set when the grant cannot be applied as such (else "")."""
    d = cfg.get("delivery") or {}
    out = []
    repos, lines = auto_merge_repos(sections(charter)["policies"])
    have = sorted({str(r) for r in (d.get("auto_merge_repos") or [])})
    if lines and repos is None:
        out.append({"key": AUTO_MERGE, "want": None, "have": have, "line": " / ".join(lines), "lines": lines,
                    "problem": "the Policies line names no owner/repo and is not 'none'"})
    elif repos is not None and repos != have:
        out.append({"key": AUTO_MERGE, "want": repos, "have": have,
                    "line": " / ".join(lines) or "Auto-merge repositories: none", "lines": lines,
                    "problem": "auto-merge is report-only: the daemon never writes it; the coordinator sets it "
                               "with config_set in the turn carrying the user's word"})
    branch = str(d.get("push_branch") or "").strip()
    grant = _push_grant(charter, branch) if branch else ""
    if grant:
        from .project import push_allowed
        no = forbidden(charter, branch)
        if not push_allowed(d):
            out.append({"key": PUSH, "want": True, "have": False, "line": grant, "lines": [grant],
                        "problem": f"the charter also says \"{no[:200]}\"" if no else ""})
        if not settinghold._allow_protected(cfg) and settinghold._refuses_push_branch(cfg, repo):
            out.append({"key": PROTECTED, "want": True, "have": False, "line": grant, "lines": [grant],
                        "problem": f"the charter also says \"{no[:200]}\"" if no else ""})
    return out


def _items(v) -> set[str]:
    """A setting's value as the set of things it grants: the repos of a list, else the value itself."""
    return {str(x) for x in v} if isinstance(v, (list, tuple, set)) else {str(v)}


def _fp(c: dict) -> str:
    return "grant:" + hashlib.sha256(f"{c['key']}\n{c['want']}\n{c['have']}\n{_flat(c['line'])}\n{c['problem']}"
                                     .encode()).hexdigest()[:16]


def _defect(db, c: dict, why: str, now: float) -> bool:
    """One keyed event per setting and state; False when it was raised before."""
    fp = _fp(c)
    if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return False
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
         (now, "harness", EVENT, fp, "normal",
          f"Harness defect: the charter grants {c['key']} = {c['want']} (\"{c['line'][:300]}\") but the config has "
          f"{c['have']}, and the daemon did not reconcile it: {why}. Not an ask: fix the charter or the config "
          f"(a user-only setting changes in the turn that carries the user's word), or settle the conflict.",
          "queued"))
    return True


def reconcile(p, db, cfg: dict, now: float, repo: Path | None = None) -> list[str]:
    """Compare the charter's grants with the live delivery settings and apply each push grant the user's
    own word on record grants (auto-merge is never written); raise a keyed defect for each one it cannot
    apply. Returns what it applied (one line each); a re-run with nothing changed applies and raises nothing."""
    from .ends import CHARTER_HISTORY
    try:
        charter = p.charter_path.read_text()
    except OSError:
        return []
    found = checks(cfg, charter, repo)
    if not found:
        return []
    try:
        history = (p.harness / CHARTER_HISTORY).read_text()
    except OSError:
        history = ""
    done = []
    for c in found:
        if c["problem"]:
            _defect(db, c, c["problem"], now)
            continue
        at = _flat(c["line"])
        applied = (db.kv(APPLIED_KV) or {}).get(c["key"], {}).get(at, [])
        missing = _items(c["want"]) - _items(c["have"])
        back = sorted(missing & set(applied))
        if back:
            # Applied once already and set back since: that later change wins, never flipped again.
            _defect(db, {**c, "problem": "set back"}, f"it was applied once ({', '.join(back)}) and has been set "
                                                      "back since; the later setting wins and is not changed again",
                    now)
            continue
        words = [user_word(db, charter, history, line) for line in c["lines"]]
        who = "" if not words or not all(words) else ", ".join(dict.fromkeys(words))
        if not who:
            if c["key"] != PROTECTED:   # settinghold.setup_question asks about that one, once
                _defect(db, c, "no user message on record grants it (the Brief does not say it, and "
                               "CHARTER.history.md logs the Policies line without a user message)", now)
            continue
        try:
            p.set_config(c["key"], c["want"], by=configwho.by("user", msgs=configwho.msg_ids(who), via=f"grant: {who}"[:80]))
        except Exception as e:   # a config it cannot write is a defect, never a crash of the tick
            _defect(db, c, f"writing the config failed: {e}", now)
            continue
        name = c["key"].rpartition(".")[2]
        closed = [r["id"] for r in db.q("SELECT id FROM messages WHERE direction='out' AND kind='ask' AND handled=0 "
                                        "AND text LIKE ?", (f"%{name}%",))] if c["key"] == PROTECTED else []
        if closed:
            db.x(f"UPDATE messages SET handled=1 WHERE id IN ({','.join('?' * len(closed))})", closed)
        msg = (f"Set {c['key']} to {c['want']} (was {c['have']}): the charter grants it (\"{c['line'][:300]}\"), "
               f"on your word in {who}." + (f" Closed ask #{', #'.join(map(str, closed))}, which asked the same."
                                            if closed else ""))
        db.post("out", msg[:1500], chat=None, kind="alert", severity="low")
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "harness", APPLIED, _fp(c), "low", msg[:1500] + " Do not ask the user about it.", "queued"))
        record = db.kv(APPLIED_KV) or {}
        mine = record.setdefault(c["key"], {})
        mine[at] = sorted(set(mine.get(at, [])) | missing)
        db.set_kv(APPLIED_KV, record)
        done.append(msg)
    return done
