# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The settled-areas register: designs the coordinator settled after a review_area_cap.

A review_area_cap (reviewcap.py) ends in a decision such as "narrowed; gaps documented; queue no
more edge-case fixes there". Kept as memory decisions, they are the newest entries, so they crowd
older decisions made on the user's word out of the coordinator's memory budget, and the daily
archive then retires those. Here each settlement is one record in harness/settled_areas.json
(committed like memory): the file(s), the functions if known, a one-line rule, the decision text,
the failed review ids, created and expires (DEFAULT_DAYS after created unless given). A record
without an expiry of its own counts for `overlap.settled_days` (DEFAULT_DAYS) after it was created,
so older records written with a longer default lapse too.

The coordinator's digest carries one short line for the register (digest_line). A record's rule
and decision reach a prompt only where they apply: the coordinator's turn notes when a task spec it
adds or updates names a settled file (turn_note; a note, never a refusal), and a worker's or
reviewer's prompt when its spec or reviewed diff touches one (prompt_block); a settled area never
chains or holds a task (overlap.chain). A file is matched by
its base name, so `landwait.py` matches `runtime/ttp/landwait.py`.

migrate() moves existing memory entries into the register and archives them from memory, keeping
each entry's whole text as the record's decision, so nothing is lost.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

FILE = "settled_areas.json"
DEFAULT_DAYS = 14
KIND = "settled_area"          # memory_add's memory_kind that adds a record instead of a memory entry
RULE_CHARS, DECISION_CHARS, PROMPT_CHARS = 240, 1500, 4000
# A file name a decision names: a path or base name with a source-like extension.
FILE_RE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:py|md|sh|json|jsonl|toml|ya?ml|js|ts|tsx|html|"
                     r"css|rs|go|c|cc|cpp|h|hpp|txt))(?![\w-])")
FUNC_RE = re.compile(r"\b([A-Za-z_][\w.]*)\(\)")
REVIEWS_RE = re.compile(r"\breviews?\s+((?:(?:pooled\s+|and\s+)?#\d+[,;]?\s*)+)", re.I)
# A memory entry that records a review-area settlement (migrate's default pick).
AREA_ENTRY_RE = re.compile(r"review[_ -]area[_ -]caps?\b", re.I)
_LEAD_RE = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}\s+decision\s*)?\([^)]*\)\s*:\s*", re.I)


def path(p) -> Path:
    return p.harness / FILE


def load(p) -> list[dict]:
    """Every record, live or expired, oldest first. An unreadable register reads as empty."""
    try:
        data = json.loads(path(p).read_text())
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def days(p) -> float:
    """How long a record without an expiry of its own counts: `overlap.settled_days`, else DEFAULT_DAYS."""
    try:
        return float(((p.config().get("overlap") or {}) if p is not None else {}).get("settled_days") or DEFAULT_DAYS)
    except (TypeError, ValueError):
        return float(DEFAULT_DAYS)


def _until(r: dict, max_days: float) -> float:
    """When record `r` lapses: its own expiry if it was given one, else the earlier of its stored
    expiry and created + max_days; 0 for none."""
    until = float(r.get("expires") or 0)
    if not r.get("expires_given") and r.get("created"):
        cap = float(r["created"]) + max_days * 86400
        until = min(until, cap) if until else cap
    return until


def live(p, now: float | None = None) -> list[dict]:
    """The records still in force, each with its effective `expires` (_until)."""
    now = time.time() if now is None else now
    n = days(p)
    out = []
    for r in load(p):
        until = _until(r, n)
        if not until or until > now:
            out.append({**r, "expires": until} if until else r)
    return out


def _clean(xs) -> list[str]:
    xs = [xs] if isinstance(xs, str) else list(xs or [])
    out: list[str] = []
    for x in xs:
        for part in str(x).replace(",", " ").split():
            if part and part not in out:
                out.append(part)
    return out


def _reviews(xs) -> list[int]:
    out: list[int] = []
    for x in [xs] if isinstance(xs, (str, int)) else list(xs or []):
        for n in re.findall(r"\d+", str(x)):
            if int(n) not in out:
                out.append(int(n))
    return out


def one_line(text: str, limit: int = RULE_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def add(p, files, rule: str, decision: str = "", reviews=(), functions=(), created: float | None = None,
        expires: float | None = None, key: str | None = None, source: str | None = None,
        commit: bool = True) -> dict:
    """Add one record and commit the register. A record added again under the same `key` (a
    replayed coordinator turn) or `source` (a migrated memory entry) is kept once."""
    from .forbidden import check
    from .project import write_json
    files, rule = _clean(files), one_line(rule)
    if not rule:
        raise ValueError(f"{KIND}: give a one-line `rule` (or `text`) for the settled area")
    decision = " ".join(str(decision or "").split()) or rule
    check(p, "settled area", f"{rule}\n{decision}", " ".join(files))
    records = load(p)
    for r in records:
        if (key and r.get("key") == key) or (source and r.get("source") == source):
            return r
    created = time.time() if created is None else float(created)
    n = 1 + max((int(m[1]) for r in records if (m := re.fullmatch(r"sa(\d+)", str(r.get("id") or "")))), default=0)
    rec = {"id": f"sa{n}",
           "files": files, "functions": _clean(functions), "rule": rule, "decision": decision,
           "reviews": _reviews(reviews), "created": created,
           "expires": float(expires) if expires else created + days(p) * 86400}
    if expires:
        rec["expires_given"] = True
    if key:
        rec["key"] = key
    if source:
        rec["source"] = source
    records.append(rec)
    write_json(path(p), records)
    if commit:
        p.commit_harness([path(p)], f"settled area {rec['id']}: {', '.join(files) or 'no file'}: {rule[:60]}")
    return rec


def from_action(p, a: dict, key: str | None = None) -> dict:
    """A record from the coordinator's `memory_add` with memory_kind settled_area: `files` (else the
    files `text` names), `title` as the one-line rule (else `text`), `text` as the decision,
    `reviews` (else the ids `text` gives as failed reviews), `expires` (else DEFAULT_DAYS)."""
    from . import ends
    text = str(a.get("text") or "")
    return add(p, a.get("files") or files_in(text), a.get("title") or text, text,
               a.get("reviews") or reviews_in(text), FUNC_RE.findall(text),
               expires=ends.from_action(a, where=p).get("expires"), key=key)


def files_in(text: str) -> list[str]:
    return list(dict.fromkeys(m for m in FILE_RE.findall(text or "") if not m.startswith(".")))


def reviews_in(text: str) -> list[int]:
    return _reviews(" ".join(m for m in REVIEWS_RE.findall(text or "")))


def _base(f: str) -> str:
    return f.rstrip("/").rsplit("/", 1)[-1].lower()


def matching(records: list[dict], text: str = "", files=()) -> list[dict]:
    """The records whose files `text` names or `files` (changed paths) touch, by base name."""
    named = {_base(f) for f in files_in(text)} | {_base(f) for f in files or ()}
    return [r for r in records if named & {_base(f) for f in r.get("files") or []}]


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))


def record_text(r: dict, decision: bool = True) -> str:
    funcs = f" ({', '.join(r['functions'])})" if r.get("functions") else ""
    revs = f"; failed reviews {', '.join(f'#{i}' for i in r['reviews'])}" if r.get("reviews") else ""
    head = (f"{'[' + r['id'] + '] ' if r.get('id') else ''}{', '.join(r.get('files') or []) or 'no file'}{funcs}: {r.get('rule')} "
            f"(settled {_when(r.get('created') or 0)}, until {_when(r.get('expires') or 0)}{revs})")
    dec = one_line(r.get("decision") or "", DECISION_CHARS)
    return head + (f" Decision: {dec}" if decision and dec and dec != r.get("rule") else "")


def digest_line(p, now: float | None = None) -> str:
    """The digest's one line for the register, or '' when it holds no live record."""
    recs = live(p, now)
    if not recs:
        return ""
    files = sorted({f for r in recs for f in r.get("files") or []}, key=_base)
    shown = ", ".join(files[:12]) + (f" +{len(files) - 12} more" if len(files) > 12 else "")
    bare = sum(1 for r in recs if not r.get("files"))
    return (f"## Settled areas ({FILE}): {len(recs)} live, files {shown or 'none'}"
            + (f", {bare} without a file" if bare else "")
            + ". A task spec naming one gets its rule as a turn note; workers and reviewers touching one get it too")


def turn_note(p, spec: str, what: str, now: float | None = None) -> str:
    """The coordinator's note when the spec it just wrote names a settled file ('' if none)."""
    hits = matching(live(p, now), spec)
    if not hits:
        return ""
    return (f"{what}: its spec names a settled area. Check the task stays inside the settled design, or "
            "re-plan it as one redesign: " + " | ".join(record_text(r) for r in hits))[:PROMPT_CHARS]


def task_files(p, task: dict) -> list[str]:
    """What a review task reviews (its changed files); [] for others or when unreadable."""
    if (task.get("kind") or "") != "review":
        return []
    from . import worktree
    try:
        return list(worktree.diff_lines(p, worktree.reviewed_refs(p, task)) or {})
    except Exception:   # a branch that is gone or unreadable touches nothing
        return []


def prompt_block(p, task: dict, now: float | None = None) -> str:
    """The worker's or reviewer's block for the settled areas its spec or diff touches ('' if none)."""
    recs = live(p, now)
    if not recs:
        return ""
    hits = matching(recs, f"{task.get('title') or ''}\n{task.get('spec') or ''}", task_files(p, task))
    if not hits:
        return ""
    body = "\n".join(f"- {record_text(r)}" for r in hits)
    return ("## Settled areas this task touches\nThe coordinator settled these designs after repeated failed "
            "reviews. Keep changes inside them: no new edge-case fixes there, and judge a change against the "
            "settled rule and its accepted gaps, not against gaps it documents.\n" + body)[:PROMPT_CHARS]


def candidates(p) -> list[dict]:
    """Live memory decisions that record a review-area settlement (migrate's default pick)."""
    return [e for e in p._memory_entries() if e["kind"] == "decision" and not e["standing"]
            and AREA_ENTRY_RE.search(e["line"])]


def _entry_record(e: dict, body: str) -> dict:
    text = " ".join(body.split())
    rule = _LEAD_RE.sub("", text, count=1) or text
    from .project import _stamp_ts
    return {"files": files_in(text), "functions": FUNC_RE.findall(text), "rule": one_line(rule),
            "decision": text, "reviews": reviews_in(text), "created": _stamp_ts(e["created"]) or time.time(),
            "expires": (e.get("end") or {}).get("expires"), "source": e["name"]}


def migrate(p, names: list[str] | None = None, dry_run: bool = False) -> tuple[list[dict], list[str]]:
    """Move memory entries `names` (default: candidates()) into the register and archive them, with
    one harness commit: (records, errors). A name that is unknown, archived, standing or not a
    decision or fact is reported and skipped. A dry run writes nothing and returns the records it
    would add."""
    from .project import ARCHIVABLE_MEMORY_KINDS, _memory_parts
    entries = {e["name"]: e for e in p._memory_entries()}
    picked, errors = [], []
    for raw in names if names else [e["name"] for e in candidates(p)]:
        name = Path(str(raw).strip().strip("[]")).stem
        try:
            name, archived = p._resolve_memory_name(name)
        except ValueError as ex:
            errors.append(str(ex))
            continue
        e = entries.get(name)
        if archived or e is None:
            errors.append(f"[{name}] is archived already; nothing moved")
        elif e["standing"] or e["kind"] not in ARCHIVABLE_MEMORY_KINDS:
            errors.append(f"[{name}] is a {'standing ' if e['standing'] else ''}{e['kind']}: only plain decisions "
                          "and facts move to the register")
        elif name not in [x["name"] for x, _ in picked]:
            picked.append((e, _memory_parts((p.memory_dir / f"{name}.md").read_text())[1]))
    recs = [_entry_record(e, body) for e, body in picked]
    if dry_run or not recs:
        return recs, errors
    out = [add(p, r["files"], r["rule"], r["decision"], r["reviews"], r["functions"], created=r["created"],
               expires=r["expires"], source=r["source"], commit=False) for r in recs]
    done, errs = p.forget_memories([r["source"] for r in recs], why=f"moved to the settled-areas register ({FILE})",
                                   commit=False)
    errors += errs
    moved = [d.stem for d in done]
    p.commit_harness([path(p), p.memory_index, *done], f"settled areas: moved {len(moved)} memory entr"
                     f"{'y' if len(moved) == 1 else 'ies'} into {FILE}",
                     removed=[p.memory_dir / f"{n}.md" for n in moved])
    return out, errors
