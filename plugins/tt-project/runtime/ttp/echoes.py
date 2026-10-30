"""Retired-rule echoes: text the user retired that still binds through a leftover copy.

When a charter_update replaces text (`quote`, `replaces`), a memory_add `supersedes` entries or a
user Correction fact names a wrong value, `record` keeps the retired text's distinctive phrases (its
quoted spans, config keys and identifiers, its first sentence; for a Correction, the wrong part when
it is prose). Each coordinator turn `digest_lines` searches, model-free, every project-local place
the old text can still bind (live memory entries, charter sections, schedules' payloads with their
prechecks and heal checks, open tasks' specs and start_when probes, config values, and the project's own
files: harness scripts, tools, hooks and prompts, the project's skills and agent instructions, and the
paths in coordinator.echo_paths) and lists the hits under RETIRED_HEADER, which carries its own
instruction; memory entries that hit are also named on one line for memory_forget, so they do not pile up. It is a notice only: no event, wake, hold or model call. Not searched: the replacing
text itself (its entry, or its words wherever they stand) and the archives (memory/archive, the charter
history), and harness files the project never edited (equal to the harness's `upstream` snapshot or the
plugin template): only a project-tuned copy can be fixed. A phrase the replacing text keeps is no phrase of the
retired rule, nor is plugin vocabulary (one identifier or `ttp <command>` name found in the runtime source or an
unedited template file, such as `start_when` or `ttp list`; never the plugin's prose, nor a Correction's
wrong part): alone it matches unrelated text. A hit still there a turn
after it was first shown is marked. An audit record that only preserves an old assertion is declared
historical in HISTORICAL_INDEX (its path, sha256 and the retirement's id): while its bytes still hash
to that value and the retirement is live, that retirement's hit on it is counted, not listed; any
mismatch lists it as before. Binding places (prompts, harness tools, skills, agent instructions,
executables) are never historical. Accepted gap: a task whose spec quotes the retired rule in order
to remove it is listed too.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

RETIRED_KEY = "retired_rules"        # kv: [{"id", "at", "source", "old", "new", "phrases", "skip", "negated"}]
SEEN_KEY = "retired_echo_seen"       # kv: {"turn": last_coordinator_turn, "hits": {hit key: turns shown},
                                     #      "historical": hits declared historical, not listed}
HISTORICAL_INDEX = "echo_historical.json"   # harness/: {"historical": [{"path", "sha256", "retirement"}]}
HISTORICAL_MAX = 200                 # index entries read
_BINDING_DIRS = {"bin", "prompts", "hooks", "tools"}   # harness/ folders of live scripts and prompts
_INSTRUCTIONS = {"CLAUDE.md", "AGENTS.md", "SKILL.md"}
_SHA256 = re.compile(r"[0-9a-f]{64}")
RETIRED_DAYS = 30
RETIRED_KEEP = 50
MAX_PHRASES = 8
SECTION_LINES = 10                   # the whole section, its header included
LINE_CHARS = 160
FILE_MAX = 400                       # files read per scan
FILE_BYTES = 256 * 1024              # a bigger file is read up to here
# harness/ entries not read as files: searched in their own form (memory entries, charter sections,
# schedules, config), archives, or the installed runtime and run state.
HARNESS_SKIP = {"memory", "runtime", "CHARTER.md", "CHARTER.history.md", "MEMORY.md", "project.json",
                "schedules.json", "settled_areas.json", ".git", "__pycache__", "node_modules"}
# Below the project root: the project's own skills, hooks and agent instructions.
ROOT_PATHS = (".claude/skills", ".claude/hooks", ".claude/commands", ".claude/agents", ".agents/skills",
              "CLAUDE.md", "AGENTS.md")
_TEMPLATE = Path(__file__).resolve().parents[2] / "template"   # the plugin template, when run from the plugin
_RUNTIME: dict[str, set[str]] = {}   # "vocab": the runtime source's and plugin template's identifiers, read once
RETIRED_HEADER = ("## Retired-rule echoes (text the user retired still binds here; fix each this turn: memory_forget "
                  "or memory_add `supersedes`, charter_update `quote`, schedule_set, task_update, config_set; a "
                  "notice, it never waits)")

_QUOTED = re.compile(r'"([^"\n]{6,300})"|“([^”\n]{6,300})”|`([^`\n]{4,200})`'
                     r"|(?:(?<=\s)|^)'([^'\n]{12,300})'(?=[\s.,;:)]|$)")
_IDENT = re.compile(r"\b[a-z][a-z0-9]*(?:[._][a-z0-9]+)+\b", re.I)
_TTP_CMD = re.compile(r"\bttp [a-z][a-z-]*[a-z]\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_NEGATION = re.compile(r"\b(?:not|never|no longer|instead of|rather than|isn't|aren't|wasn't|weren't|retires)\s*$")


def _flat(text: Any) -> str:
    return " ".join(str(text or "").split()).lower()


def phrases(old: str, new: str = "") -> list[str]:
    """The distinctive phrases of retired text `old`, lower-case: quoted and backticked spans, config
    keys and identifiers (a dot or underscore inside), and its first sentence (its first 12 words), at
    most MAX_PHRASES. One the replacing text `new` holds too is left out: both rules say it."""
    keep = _flat(new)
    found: list[str] = []
    for m in _QUOTED.finditer(old or ""):
        found.append(next(g for g in m.groups() if g))
    found += [m.group() for m in _IDENT.finditer(old or "") if len(m.group()) >= 8]
    first = _SENTENCE.split(" ".join((old or "").split()), 1)[0].rstrip(".!?")
    words = first.split()
    if len(words) >= 4:
        found.append(" ".join(words[:12]))
    out: list[str] = []
    for f in map(_flat, found):
        f = f.strip(" .,;:")
        if len(f) >= 6 and f not in out and f not in keep:
            out.append(f)
    return out[:MAX_PHRASES]


def _store(db, rec: dict, now: float) -> None:
    recs = [r for r in db.kv(RETIRED_KEY, []) or [] if now - r.get("at", 0) < RETIRED_DAYS * 86400
            and r.get("id") != rec["id"]]
    db.set_kv(RETIRED_KEY, (recs + [rec])[-RETIRED_KEEP:])


def record(db, source: str, old: str, new: str = "", skip: list[str] = (), now: float | None = None,
           negated: bool = False, found: list[str] | None = None) -> dict | None:
    """Keep retired text `old` (replaced by `new`) to search for. `skip`: place keys (see places) never
    searched for it, such as the replacing entry. `found`: the phrases, when the caller has them.
    `negated`: a match right after a negation ('never <phrase>') agrees with the change, so is no hit.
    None (nothing kept) when the text has no distinctive phrase."""
    now = time.time() if now is None else now
    ps = phrases(old, new) if found is None else [p for p in map(_flat, found) if len(p) >= 6][:MAX_PHRASES]
    if not ps:
        return None
    rec = {"id": hashlib.sha256(f"{source}\n{_flat(old)}".encode()).hexdigest()[:10], "at": now,
           "source": " ".join(source.split())[:200], "old": " ".join((old or "").split())[:600],
           "new": _flat(new)[:4000], "phrases": ps, "skip": list(skip), "negated": negated}
    _store(db, rec, now)
    return rec


def record_correction(db, name: str, text: str, now: float | None = None) -> dict | None:
    """A user Correction entry `name`: its wrong parts (after 'not', 'never', 'no longer', ...) that are
    prose, at least 3 words, are retired text. A bare value ('not low') is left to correction.repeated."""
    from .correction import _HEAD, _WRONG, _WRONG_END
    head = _HEAD.match(text or "")
    if not head:
        return None
    body = text[head.end():]
    wrong = [_WRONG_END.split(m.group(1))[0].strip() for m in _WRONG.finditer(body)]
    wrong = [w for w in wrong if len(w.split()) >= 3]
    return record(db, f"memory [{name}] (user correction)", "; ".join(wrong), skip=[f"memory:{name}"], now=now,
                  negated=True, found=wrong) if wrong else None


def places(p, pristine: list[str] | None = None) -> list[tuple[str, str, str]]:
    """Every project-local place retired text can still bind, as (key, label, text): live memory entries
    (the one store both workers' and the coordinator's copies come from), charter sections, schedules'
    payload strings and descriptions, open tasks' specs and start_when probes, config string values, and
    the project's files. Harness files equal to the template are no place; their text goes to `pristine`."""
    from .db import TERMINAL_TASK_STATES, deferral
    from .prompts import charter_sections
    out: list[tuple[str, str, str]] = []
    for e in p._memory_entries():
        out.append((f"memory:{e['name']}", f"memory [{e['name']}]", e["body"]))
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    for head, body in charter_sections(charter):
        name = head.lstrip("#").strip() or "(top)"
        out.append((f"charter:{name}", f"charter section '{name}'", "\n".join(body)))

    def walk(key: str, label: str, val: Any, path: str) -> None:
        if isinstance(val, str):
            out.append((f"{key}:{path}", f"{label} {path}".strip(), val))
        elif isinstance(val, dict):
            for k, v in val.items():
                walk(key, label, v, f"{path}.{k}" if path else str(k))
        elif isinstance(val, list):
            for i, v in enumerate(val):
                walk(key, label, v, f"{path}[{i}]")
    for s in p.db.q("SELECT name, description, payload FROM schedules ORDER BY name"):
        try:
            payload = json.loads(s["payload"] or "{}")
        except ValueError:
            payload = s["payload"]
        walk(f"schedule:{s['name']}", f"schedule {s['name']}", {"description": s["description"] or "",
                                                                  "payload": payload}, "")
    for t in p.db.q(f"SELECT id, spec, labels FROM tasks WHERE status NOT IN "
                    f"({','.join('?' * len(TERMINAL_TASK_STATES))}) ORDER BY id", TERMINAL_TASK_STATES):
        out.append((f"task:{t['id']}:spec", f"task #{t['id']} spec", t["spec"] or ""))
        when = deferral(t).get("when")
        if when:
            out.append((f"task:{t['id']}:start_when", f"task #{t['id']} start_when", when))
    try:
        cfg = p.config()
    except Exception:   # a broken config is reported elsewhere; the notice never fails the turn
        cfg = {}
    walk("config", "config", cfg, "")
    extra = (cfg.get("coordinator") or {}).get("echo_paths") if isinstance(cfg, dict) else None
    out += _files(p, [x for x in extra if isinstance(x, str)] if isinstance(extra, list) else [],
                  pristine if pristine is not None else [])
    return out


def _upstream(harness: Path) -> dict[str, str]:
    """The harness's `upstream` snapshot (the pristine template) as {path: blob sha}; {} without one."""
    from . import gitmemo
    args = ("ls-tree", "-r", "upstream")
    try:
        r = subprocess.run(["git", "-C", str(harness), *args], capture_output=True, text=True,
                           timeout=gitmemo.timeout(10, args))
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for ln in r.stdout.splitlines() if r.returncode == 0 else ():
        meta, _, path = ln.partition("\t")
        if meta.split()[1:2] == ["blob"]:
            out[path] = meta.split()[2]
    return out


def _template_copy(harness: Path, upstream: dict[str, str], real: Path, data: bytes) -> bool:
    """Whether harness file `real` (all of it in `data`) is the template unedited: its upstream blob or
    the plugin template's file (prompts/ and bin/ come from template/prompts and template/bin)."""
    try:
        rel = real.relative_to(harness.resolve()).as_posix()
    except ValueError:
        return False
    if upstream.get(rel) == hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest():
        return True
    try:
        return (_TEMPLATE / rel).read_bytes() == data
    except OSError:
        return False


def _files(p, extra: list[str], pristine: list[str]) -> list[tuple[str, str, str]]:
    """The project's own text files as places (key "file:<path>"): harness/ less HARNESS_SKIP, ROOT_PATHS
    and `extra` (relative to the project root, never outside it), at most FILE_MAX files of FILE_BYTES.
    A harness file equal to the template is no place (the project cannot fix it): its text goes to
    `pristine`, as plugin vocabulary."""
    root = Path(p.root).resolve()
    hroot = Path(p.harness)
    upstream = _upstream(hroot)
    starts = [(Path(p.harness), True)] + [(root / x, False) for x in (*ROOT_PATHS, *extra)]
    out: list[tuple[str, str, str]] = []
    seen: set[Path] = set()

    def take(path: Path) -> bool:
        if len(out) >= FILE_MAX:
            return False
        try:
            real = path.resolve()
            if real in seen or not real.is_relative_to(root) or not path.is_file():
                return True
            seen.add(real)
            with open(path, "rb") as fh:
                data = fh.read(FILE_BYTES)
        except OSError:
            return True
        if b"\0" in data[:4096]:
            return True
        if len(data) < FILE_BYTES and _template_copy(hroot, upstream, real, data):
            pristine.append(_flat(data.decode("utf-8", "replace")))
            return True
        rel = real.relative_to(root).as_posix()
        out.append((f"file:{rel}", f"file {rel}", data.decode("utf-8", "replace")))
        return True

    for start, harness in starts:
        try:
            if not start.exists() or not start.resolve().is_relative_to(root):   # never walk outside the root
                continue
        except OSError:
            continue
        if start.is_file():
            if not take(start):
                break
            continue
        for d, dirs, names in os.walk(start):
            top = Path(d) == start
            dirs[:] = sorted(x for x in dirs if x not in (HARNESS_SKIP if harness and top else {".git", "__pycache__",
                                                                                                "node_modules"}))
            for n in sorted(names):
                if harness and top and n in HARNESS_SKIP:
                    continue
                if not take(Path(d) / n):
                    return out
    return out


def _line(text: str, phrase: str) -> str:
    """The line of `text` holding `phrase`, clipped around it."""
    for ln in text.splitlines():
        if phrase in _flat(ln):
            ln = " ".join(ln.split())
            break
    else:
        ln = " ".join(text.split())
    at = ln.lower().find(phrase.split()[0]) if phrase.split() else 0
    start = max(0, min(at - 40, len(ln) - LINE_CHARS)) if len(ln) > LINE_CHARS else 0
    cut = ln[start:start + LINE_CHARS]
    return ("..." if start else "") + cut + ("..." if start + LINE_CHARS < len(ln) else "")


def _lineno(text: str, phrase: str) -> int:
    """The 1-based line of `text` holding `phrase` (1 when it spans lines)."""
    for i, ln in enumerate(text.splitlines(), 1):
        if phrase in _flat(ln):
            return i
    return 1


def _vocab(text: str) -> set[str]:
    """The plugin identifiers in flattened `text`: names with a dot or underscore inside (action types,
    config keys, task and schedule fields) and `ttp <command>` names. Never its prose."""
    return {m.group() for m in _IDENT.finditer(text)} | {m.group() for m in _TTP_CMD.finditer(text)}


def _generic(ph: str, extra: set[str] | list[str] = ()) -> bool:
    """Whether phrase `ph` is plugin vocabulary: one identifier or `ttp <command>` name found in the
    runtime source or the plugin template, or in `extra` (the unedited template files' _vocab)."""
    ph = ph.strip("`'\" ")
    if len(ph.split()) > 2 or not (_IDENT.fullmatch(ph) or _TTP_CMD.fullmatch(ph)):
        return False
    if "vocab" not in _RUNTIME:   # read once per process
        src = []
        for f in [*sorted(Path(__file__).resolve().parent.glob("*.py")),
                  *(sorted(_TEMPLATE.rglob("*")) if _TEMPLATE.is_dir() else [])]:
            try:
                if f.is_file() and f.stat().st_size < FILE_BYTES * 4:
                    src.append(f.read_text(errors="replace"))
            except OSError:
                pass
        _RUNTIME["vocab"] = _vocab(_flat("\n".join(src)))
    return ph in _RUNTIME["vocab"] or ph in extra


def _hit(rec: dict, flat: str, phrases_: list[str] | None = None) -> str:
    """The first phrase of `rec` (or `phrases_`) found in flattened text `flat` outside its replacing
    text ('' when none)."""
    if rec.get("new") and len(rec["new"]) >= 6:
        flat = flat.replace(rec["new"], " ")
    for ph in rec.get("phrases") or [] if phrases_ is None else phrases_:
        i = flat.find(ph)
        while i >= 0:
            if not (rec.get("negated") and _NEGATION.search(flat[max(0, i - 20):i])):
                return ph
            i = flat.find(ph, i + 1)
    return ""


def _binding(real: Path, root: Path, harness: Path) -> bool:
    """Whether project file `real` can still bind (never historical): an agent instruction or skill file, an
    executable, a file below ROOT_PATHS, a top-level harness file or one in a harness folder of scripts and
    prompts."""
    if real.name in _INSTRUCTIONS or os.access(real, os.X_OK):
        return True
    rel = real.relative_to(root).as_posix()
    if any(rel == x or rel.startswith(x + "/") for x in ROOT_PATHS):
        return True
    try:
        parts = real.relative_to(harness).parts
    except ValueError:
        return False
    return len(parts) == 1 or parts[0] in _BINDING_DIRS


def historical_index(p, recs: list[dict]) -> dict[str, dict[str, set[str]]]:
    """The well-formed entries of harness/HISTORICAL_INDEX as {"file:<path>": {retirement id: {sha256}}}.
    An entry counts only with `path` the exact project-relative path of a regular, non-binding file (no
    folder, pattern, `..`, symlink or absolute path), `sha256` 64 lower-case hex digits and `retirement`
    the id of a live retirement in `recs`; anything else is ignored, so its hits stay listed."""
    try:
        data = json.loads((Path(p.harness) / HISTORICAL_INDEX).read_text())
    except (OSError, ValueError):
        return {}
    entries = data.get("historical") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return {}
    live = {r.get("id") for r in recs}
    root = Path(p.root).resolve()
    harness = Path(p.harness).resolve()
    out: dict[str, dict[str, set[str]]] = {}
    for e in entries[:HISTORICAL_MAX]:
        if not isinstance(e, dict):
            continue
        rel, sha, rid = e.get("path"), e.get("sha256"), e.get("retirement")
        if not (isinstance(rel, str) and isinstance(sha, str) and _SHA256.fullmatch(sha) and isinstance(rid, str)
                and rid in live):
            continue
        path = root / rel
        try:
            real = path.resolve()
            if (not real.is_relative_to(root) or real.relative_to(root).as_posix() != rel or path.is_symlink()
                    or not path.is_file() or _binding(real, root, harness)):
                continue
        except (OSError, ValueError):
            continue
        out.setdefault(f"file:{rel}", {}).setdefault(rid, set()).add(sha)
    return out


def _sha256(path: Path) -> str:
    """The sha256 of all of file `path` ('' when unreadable)."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def scan(p, now: float | None = None, historical: list[dict] | None = None) -> list[dict]:
    """The retired texts' echoes: [{"key", "label", "line", "phrase", "source", "retirement"}], newest
    retirement first. A hit on a file the historical index declares historical for that retirement, with
    its bytes still hashing to the recorded value, goes to `historical` instead."""
    now = time.time() if now is None else now
    recs = [r for r in p.db.kv(RETIRED_KEY, []) or [] if now - r.get("at", 0) < RETIRED_DAYS * 86400]
    if not recs:
        return []
    index = historical_index(p, recs)
    digests: dict[str, str] = {}
    pristine: list[str] = []
    where = [(key, label, text, _flat(text)) for key, label, text in places(p, pristine)]   # flattened once
    vocab = set().union(*map(_vocab, pristine))
    out, seen = [], set()
    for rec in sorted(recs, key=lambda r: -r.get("at", 0)):
        own = [ph for ph in rec.get("phrases") or [] if rec.get("negated") or not _generic(ph, vocab)]
        if not own:
            continue
        skip = set(rec.get("skip") or ())
        for key, label, text, flat in where:
            if key in skip or (rec["id"], key) in seen:
                continue
            ph = _hit(rec, flat, own)
            if ph:
                seen.add((rec["id"], key))
                if key.startswith("file:"):
                    label += f":{_lineno(text, ph)}"
                hit = {"key": f"{rec['id']}:{key}", "label": label, "line": _line(text, ph), "phrase": ph,
                       "source": rec.get("source") or "", "retirement": rec["id"]}
                shas = index.get(key, {}).get(rec["id"])
                if shas:
                    if key not in digests:
                        digests[key] = _sha256(Path(p.root).resolve() / key[5:])
                    if digests[key] in shas:
                        if historical is not None:
                            historical.append(hit)
                        continue
                out.append(hit)
    return out


def digest_lines(p, now: float | None = None) -> list[str]:
    """The digest's Retired-rule echoes section, at most SECTION_LINES lines; [] when nothing echoes. A
    hit already shown in an earlier turn is marked. Never posts, wakes or holds anything."""
    hist: list[dict] = []
    try:
        hits = scan(p, now, hist)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):   # a notice never fails the turn
        return []
    db = p.db
    turn = db.kv("last_coordinator_turn", 0) or 0
    old = db.kv(SEEN_KEY, {}) or {}
    before = old.get("hits") or {}
    fresh = old.get("turn") != turn   # a second digest within one turn counts no turn
    counts = {h["key"]: (before.get(h["key"], 0) + 1 if fresh else before.get(h["key"]) or 1) for h in hits}
    if counts != before or old.get("turn") != turn or (old.get("historical") or 0) != len(hist):
        db.set_kv(SEEN_KEY, {"turn": turn, "hits": counts, "historical": len(hist)})
    if not hits:
        return []
    mem = list(dict.fromkeys(h["key"].split(":", 2)[2] for h in hits if h["key"].split(":", 2)[1] == "memory"))
    room = SECTION_LINES - 1 - bool(mem) - bool(hist)
    shown = hits if len(hits) <= room else hits[:room - 1]
    lines = [RETIRED_HEADER]
    for h in shown:
        n = counts[h["key"]]
        rid = f", id {h['retirement']}" if h["key"].split(":", 2)[1] == "file" else ""   # for the historical index
        lines.append(f"- {h['label']}: \"{h['line']}\" (retired: {h['source']}{rid}; matches \"{h['phrase'][:60]}\")"
                     + (f" [still here, {n} turns]" if n >= 2 else ""))
    if len(shown) < len(hits):
        lines.append(f"- ... and {len(hits) - len(shown)} more; fix these and the rest show next turn")
    if mem:   # every memory entry that still holds a retired rule, shown or not
        names = ", ".join(f"[{n}]" for n in mem[:12]) + (f" and {len(mem) - 12} more" if len(mem) > 12 else "")
        lines.append(f"- memory to retire (memory_forget, or memory_add `supersedes` to keep what is still right): {names}")
    if hist:   # declared historical: counted, never silently dropped
        try:
            where = (Path(p.harness).resolve() / HISTORICAL_INDEX).relative_to(Path(p.root).resolve()).as_posix()
        except ValueError:
            where = HISTORICAL_INDEX
        lines.append(f"- {len(hist)} historical line{'s' if len(hist) != 1 else ''} not shown (declared in {where})")
    return lines
