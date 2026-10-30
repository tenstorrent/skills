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

Each retirement has a KIND: WITHDRAWN (the user withdrew or replaced a governing instruction: a
charter_update `quote` or `replaces`, a user Correction, a memory change whose `why` cites the user's
words, or one on the user's turn that cites or quotes their words or whose message names the old rule:
coordinator._withdrawn; a dedupe on the user's turn is not one), COMPACTED (a factual entry merged or shortened) or SUPERSEDED (one
coordinator decision superseded by another). Only a WITHDRAWN retirement's hit on a governing phrase
is a directive; a hit of the other kinds, or one resting only on a filename, path, version or evidence
fragment (_weak, or an identifier the retired text holds only inside such a token: _fragment), is
advisory: counted on one line, never a fix order. A fourth kind, UNVERIFIED (its origin unknown: nothing
shows the user withdrew that rule), is advisory too: a record stored before kinds existed gets one from
its source (_kind), and an unknown kind or source is UNVERIFIED. A hit of an unsourced Restrictions item the
coordinator dropped is advisory while the Brief or a sourced item still states it (restriction_audit.still_stated):
that rule still binds, so its copies are right. `export` is the read-only full list
behind `ttp echoes`. Accepted gaps (settled weak-token set): a dashed config-style name with a decimal
suffix ('max-ratio-1.5') counts as weak; a version joined by '_', '=' or '+' counts as strong (over-flag).
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

RETIRED_KEY = "retired_rules"        # kv: [{"id", "at", "source", "old", "new", "phrases", "skip", "negated", "kind"}]
SEEN_KEY = "retired_echo_seen"       # kv: {"turn": last_coordinator_turn, "hits": {hit key: turns shown},
                                     #      "historical": hits declared historical, not listed,
                                     #      "advisory": advisory hits, counted, not listed}
WITHDRAWN, COMPACTED, SUPERSEDED = "withdrawn", "compacted", "superseded"   # retirement kinds (see the top)
UNVERIFIED = "unverified"
KINDS = (WITHDRAWN, COMPACTED, SUPERSEDED, UNVERIFIED)
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
# A phrase that alone names no rule: a file name, a path, a version, a hash or an evidence reference.
_FILEISH = re.compile(r"[\w.-]*\.(?:md|markdown|txt|json|jsonl|ya?ml|toml|py|sh|bash|js|ts|log|csv|tsv|html?|ini"
                      r"|cfg|conf|lock|db|sqlite|patch|diff|out|err|rc|pdf|png|tar|gz|zip)(?::\d+)*")
_VERSION = re.compile(r"v?\d+(?:\.\d+)+(?:[-+][\w.]+)?")
# A name joined to a version by '-' or '@' only (tool-1.2.3, pkg@1.2.3); a config key=value or key_1.5 never is.
_VERSIONED = re.compile(r"[\w.]*[-@]v?\d+(?:\.\d+)+(?:[-+][\w.]+)?")
# Words that only say a version or build is in use ('running 0.2.646'): no rule beside a weak token.
_STATE = {"running", "runs", "using", "uses", "version", "versions", "release", "build", "installed", "deployed",
          "currently"}
# A state word glued to its version (no space before it) reads as the spaced form; no other word is split.
_STATE_GLUED = re.compile(r"\b(" + "|".join(sorted(_STATE, key=len, reverse=True)) + r")(v?\d+(?:\.\d+)+)\b", re.I)
_EVIDENCE = re.compile(r"(?:#\d+|[0-9a-f]*\d[0-9a-f]*|(?:msgs?|tasks?|runs?|notes?|pr|issue)\s*#?\d+|https?:\S*)")
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


def _weak_token(tok: str) -> bool:
    tok = tok.strip("`'\"()[]{}<>,;:!?")
    tok = tok.rstrip(".")
    return (not tok or "/" in tok or bool(_FILEISH.fullmatch(tok) or _VERSION.fullmatch(tok)
            or _VERSIONED.fullmatch(tok) or _EVIDENCE.fullmatch(tok) or (len(tok) >= 7 and re.fullmatch(r"[0-9a-f]+", tok)
                                            and re.search(r"\d", tok))) or not re.search(r"[a-z]", tok))


def _weak(ph: str) -> bool:
    """Whether phrase `ph` names no rule alone: each of its words of 4+ characters (all of them when none
    is) is a file name, path, version, hash, number or evidence reference ('msg #12', a commit), or, beside
    at least one such, a word that only says it is in use (_STATE: 'running 0.2.646')."""
    s = _STATE_GLUED.sub(r"\1 \2", ph.strip("`'\" "))
    for pair in re.findall(r"(?:msgs?|tasks?|runs?|notes?|pr|issue)\s+#?\d+", s):   # 'msg #12' is one reference
        s = s.replace(pair, pair.replace(" ", ""))
    words = s.split()
    long = [w for w in words if len(w.strip("`'\"()[]{}<>,;:.!?")) >= 4] or words
    weak = [w for w in long if _weak_token(w)]
    return bool(weak) and all(w in weak or w.strip("`'\"()[]{}<>,;:.!?").lower() in _STATE for w in long)


def _fragment(ph: str, old: str) -> bool:
    """Whether identifier `ph` stands in retired text `old` only inside longer weak tokens (a path, a URL, a
    versioned name: a file name at the end of a path): it keeps that token's weakness. False when it
    stands alone there even once, spans words, or is not found (the text kept is cut at 600 characters)."""
    if not ph or " " in ph:
        return False
    flat, found = _flat(old), False
    i = flat.find(ph)
    while i >= 0:
        start, end = flat.rfind(" ", 0, i) + 1, flat.find(" ", i + len(ph))
        tok = flat[start:end if end >= 0 else len(flat)]
        if tok.strip("`'\"()[]{}<>,;:!?").rstrip(".") == ph or not _weak_token(tok):
            return False
        found, i = True, flat.find(ph, i + 1)
    return found


def _kind(rec: dict) -> str:
    """Retirement `rec`'s kind: its own when it is one of KINDS, UNVERIFIED for any other. A record without
    one (stored before kinds) from its source: WITHDRAWN only where the source itself shows the user's
    withdrawal (a charter text replaced, a user correction); a memory entry superseded by another is
    SUPERSEDED, or UNVERIFIED when its text cites the user (where the rule came from, not who retired it);
    anything else (a memory_forget then recorded on a user turn alone, an unknown source) is UNVERIFIED."""
    k = rec.get("kind")
    if k is not None:
        return k if k in KINDS else UNVERIFIED
    from .project import USER_CITE
    source = rec.get("source") or ""
    if re.fullmatch(r"charter '.*' (?:quote|section) replaced|memory \[[^\]]+\] \(user correction\)", source):
        return WITHDRAWN
    if re.match(r"memory \[[^\]]+\] superseded by \[", source):
        return UNVERIFIED if USER_CITE.search(rec.get("old") or "") else SUPERSEDED
    return UNVERIFIED


def _store(db, rec: dict, now: float) -> None:
    recs = [r for r in db.kv(RETIRED_KEY, []) or [] if now - r.get("at", 0) < RETIRED_DAYS * 86400
            and r.get("id") != rec["id"]]
    db.set_kv(RETIRED_KEY, (recs + [rec])[-RETIRED_KEEP:])


def record(db, source: str, old: str, new: str = "", skip: list[str] = (), now: float | None = None,
           negated: bool = False, found: list[str] | None = None, kind: str = WITHDRAWN,
           unsourced: bool = False) -> dict | None:
    """Keep retired text `old` (replaced by `new`) to search for. `skip`: place keys (see places) never
    searched for it, such as the replacing entry. `found`: the phrases, when the caller has them.
    `negated`: a match right after a negation ('never <phrase>') agrees with the change, so is no hit.
    `kind`: one of KINDS (an unknown one counts as UNVERIFIED). `unsourced`: `old` is an advisory charter
    Restrictions item the coordinator dropped (scan checks whether a binding section still states it).
    None (nothing kept) when the text has no distinctive phrase."""
    now = time.time() if now is None else now
    ps = phrases(old, new) if found is None else [p for p in map(_flat, found) if len(p) >= 6][:MAX_PHRASES]
    if not ps:
        return None
    rec = {"id": hashlib.sha256(f"{source}\n{_flat(old)}".encode()).hexdigest()[:10], "at": now,
           "source": " ".join(source.split())[:200], "old": " ".join((old or "").split())[:600],
           "new": _flat(new)[:4000], "phrases": ps, "skip": list(skip), "negated": negated,
           "kind": kind if kind in KINDS else UNVERIFIED}
    if unsourced:
        rec["unsourced"] = True
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


def _inside(flat: str, i: int, ph: str) -> bool:
    """Whether `ph` at `flat[i:]` sits inside a longer word, name or path (a letter, digit or underscore
    beside a word-character end of it), so it is not the phrase itself."""
    before = flat[i - 1:i] if i else ""
    after = flat[i + len(ph):i + len(ph) + 1]
    return bool((re.match(r"\w", ph[:1]) and re.match(r"\w", before))
                or (re.match(r"\w", ph[-1:]) and re.match(r"\w", after)))


def _hit(rec: dict, flat: str, phrases_: list[str] | None = None) -> str:
    """The first phrase of `rec` (or `phrases_`) found as a whole in flattened text `flat` outside its
    replacing text ('' when none)."""
    if rec.get("new") and len(rec["new"]) >= 6:
        flat = flat.replace(rec["new"], " ")
    for ph in rec.get("phrases") or [] if phrases_ is None else phrases_:
        i = flat.find(ph)
        while i >= 0:
            if not (_inside(flat, i, ph) or rec.get("negated") and _NEGATION.search(flat[max(0, i - 20):i])):
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


def records(p, now: float | None = None) -> list[dict]:
    """The live retirements (kept RETIRED_DAYS), each with its kind (see _kind)."""
    now = time.time() if now is None else now
    return [dict(r, kind=_kind(r)) for r in p.db.kv(RETIRED_KEY, []) or []
            if isinstance(r, dict) and now - r.get("at", 0) < RETIRED_DAYS * 86400]


def scan(p, now: float | None = None, historical: list[dict] | None = None) -> list[dict]:
    """The retired texts' echoes: [{"key", "label", "line", "phrase", "source", "retirement", "kind",
    "class", "why"}], newest retirement first. `class` is "directive" (a WITHDRAWN retirement's governing
    phrase: fix it) or "advisory" (another kind, only a _weak or _fragment phrase matched, or a dropped
    unsourced Restrictions item the Brief or a sourced item still states: `why` says which). A hit on
    a file the historical index declares historical for that retirement, with its bytes still hashing to
    the recorded value, goes to `historical` instead (class "historical")."""
    recs = records(p, now)
    if not recs:
        return []
    index = historical_index(p, recs)
    digests: dict[str, str] = {}
    pristine: list[str] = []
    where = [(key, label, text, _flat(text)) for key, label, text in places(p, pristine)]   # flattened once
    vocab = set().union(*map(_vocab, pristine))
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    out, seen = [], set()
    for rec in sorted(recs, key=lambda r: -r.get("at", 0)):
        own = [ph for ph in rec.get("phrases") or [] if rec.get("negated") or not _generic(ph, vocab)]
        if not own:
            continue
        stated = ""
        if rec.get("unsourced"):   # a dropped advisory copy of a rule a binding section still states
            from .restriction_audit import still_stated
            stated = still_stated(charter, rec.get("old") or "")
        weak = {ph for ph in own if _weak(ph) or _fragment(ph, rec.get("old") or "")}
        own.sort(key=lambda ph: ph in weak)   # a governing phrase first: a hit rests on it when it can
        skip = set(rec.get("skip") or ())
        for key, label, text, flat in where:
            if key in skip or (rec["id"], key) in seen:
                continue
            ph = _hit(rec, flat, own)
            if ph:
                seen.add((rec["id"], key))
                if key.startswith("file:"):
                    label += f":{_lineno(text, ph)}"
                why = (f"still stated in {stated}: no action" if stated else
                       "" if rec["kind"] == WITHDRAWN and ph not in weak else
                       "origin not recorded: nothing shows the user withdrew this rule" if rec["kind"] == UNVERIFIED
                       else f"retired as {rec['kind']}, not withdrawn by the user" if rec["kind"] != WITHDRAWN
                       else "only a file name, path, version or evidence reference matches")
                hit = {"key": f"{rec['id']}:{key}", "label": label, "line": _line(text, ph), "phrase": ph,
                       "source": rec.get("source") or "", "retirement": rec["id"], "kind": rec["kind"],
                       "class": "advisory" if why else "directive", "why": why}
                shas = index.get(key, {}).get(rec["id"])
                if shas:
                    if key not in digests:
                        digests[key] = _sha256(Path(p.root).resolve() / key[5:])
                    if digests[key] in shas:
                        if historical is not None:
                            historical.append(dict(hit, **{"class": "historical"}))
                        continue
                out.append(hit)
    return out


def digest_lines(p, now: float | None = None) -> list[str]:
    """The digest's Retired-rule echoes section, at most SECTION_LINES lines; [] when nothing echoes. A
    hit already shown in an earlier turn is marked. Never posts, wakes or holds anything."""
    hist: list[dict] = []
    try:
        found = scan(p, now, hist)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):   # a notice never fails the turn
        return []
    hits = [h for h in found if h["class"] == "directive"]
    advisory = len(found) - len(hits)
    db = p.db
    turn = db.kv("last_coordinator_turn", 0) or 0
    old = db.kv(SEEN_KEY, {}) or {}
    before = old.get("hits") or {}
    fresh = old.get("turn") != turn   # a second digest within one turn counts no turn
    counts = {h["key"]: (before.get(h["key"], 0) + 1 if fresh else before.get(h["key"]) or 1) for h in hits}
    if (counts != before or old.get("turn") != turn or (old.get("historical") or 0) != len(hist)
            or (old.get("advisory") or 0) != advisory):
        db.set_kv(SEEN_KEY, {"turn": turn, "hits": counts, "historical": len(hist), "advisory": advisory})
    if not hits:   # advisory and historical hits alone order nothing: no section
        return []
    mem = list(dict.fromkeys(h["key"].split(":", 2)[2] for h in hits if h["key"].split(":", 2)[1] == "memory"))
    room = SECTION_LINES - 1 - bool(mem) - bool(hist) - bool(advisory)
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
    if advisory:   # counted, never a fix order
        lines.append(f"- {advisory} advisory match{'es' if advisory != 1 else ''} not shown: a compacted fact, a "
                     f"superseded coordinator decision, a retirement of unknown origin or only a file name, version "
                     f"or evidence reference, never a rule the user withdrew; change nothing for them (all: "
                     f"`ttp echoes`)")
    return lines


def export(p, now: float | None = None) -> dict:
    """Every live retirement and every hit, read-only (nothing stored, posted or counted):
    {"retirements": [{"id", "kind", "at", "source", "retired", "replaced_by", "phrases"}],
    "matches": [{"retirement", "where", "key", "phrase", "line", "class", "action", "why"}], "counts"}.
    `action` is "fix" for a directive, "none" for an advisory or historical hit."""
    hist: list[dict] = []
    found = scan(p, now, hist)
    recs = sorted(records(p, now), key=lambda r: -r.get("at", 0))
    matches = [{"retirement": h["retirement"], "where": h["label"], "key": h["key"].split(":", 1)[1],
                "phrase": h["phrase"], "line": h["line"], "class": h["class"],
                "action": "fix" if h["class"] == "directive" else "none", "why": h.get("why") or ""}
               for h in found + hist]
    counts = {c: sum(m["class"] == c for m in matches) for c in ("directive", "advisory", "historical")}
    return {"retirements": [{"id": r.get("id"), "kind": r["kind"], "at": r.get("at"), "source": r.get("source") or "",
                             "retired": r.get("old") or "", "replaced_by": r.get("new") or "",
                             "phrases": r.get("phrases") or []} for r in recs],
            "matches": matches, "counts": counts}


def export_text(data: dict, tz: str | None = None) -> str:
    """`ttp echoes` without --json: each retirement, then its matches."""
    from . import timefmt
    if not data["retirements"]:
        return "no live retirements"
    c = data["counts"]
    out = [f"{len(data['retirements'])} retirements; matches: {c['directive']} to fix, {c['advisory']} advisory, "
           f"{c['historical']} historical"]
    by: dict[str, list[dict]] = {}
    for m in data["matches"]:
        by.setdefault(m["retirement"], []).append(m)
    for r in data["retirements"]:
        at = timefmt.long(r["at"], tz) if isinstance(r.get("at"), (int, float)) else "?"
        out.append(f"{r['id']} [{r['kind']}] {at} {r['source']}: \"{r['retired'][:160]}\"")
        for m in by.get(r["id"], []):
            out.append(f"  - {m['class']}: {m['where']}: \"{m['line']}\" (matches \"{m['phrase'][:60]}\")"
                       + (f" ({m['why']})" if m["why"] else ""))
    return "\n".join(out)
