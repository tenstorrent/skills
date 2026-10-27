"""Requested cells: a task spec's `cells:` line against the cells a done hand-off reports finished.

A spec that asks for a set of measurements or tests (sweep levels, a test grid, model x machine
cells) names them on a line that starts with `cells:` (an optional list bullet before it), separated
by commas or semicolons. Several such lines add up. Cells come only from that explicit line, never
from titles or prose: a false 'partial' costs repeat runs. A line counts only when it is enumerable
(enumerable()): every part a short name, at most NAME_WORDS words and NAME_CHARS chars, no brackets,
no sentence end, and a ':' only inside one space-free token such as a PCI id `81:00.0`. A descriptive
line ('one row per chip: unit, cause') is refused at spec time (problem()) and, stored from before,
ignored (ignored()). A done hand-off lists the requested cells
it finished with a valid result in its `cells` field, a list of names or a dict name -> row; a cell
that ran without a valid result is not finished (a dict value that is None, False, empty, `valid:
false` or a string starting `invalid`; a 0 is a measurement, so finished). Cells match case-insensitively, ignoring whitespace and surrounding quotes; cells beyond
the requested ones are fine.
"""
from __future__ import annotations

import re

FIELD = "cells"   # the hand-off field: the requested cells finished with a valid result
LINE = re.compile(r"(?im)^[ \t]*(?:[-*][ \t]+)?cells[ \t]*:(.*)$")
SEP = re.compile(r"[,;]")
PARTIAL = "partial:"
UNREPORTED = "cells unreported:"
NAMED_CHARS = 300   # the missing cells named in a summary prefix; the follow-up lists them all
SPEC_CHARS = 4000   # the follow-up's copy of its parent's spec
NAME_WORDS = 5      # a cell name's most words ('model A x 4 chips' is 5)
NAME_CHARS = 40     # a cell name's most characters
TOKEN_COLON = re.compile(r"[^\s:]+(?::[^\s:]+)+")   # a ':' only inside one token: 81:00.0, 0000:81:00.0


def norm(cell) -> str:
    """The form cells compare in: no whitespace, no surrounding quotes or backticks, case folded."""
    return "".join(str(cell).split()).strip("`'\"").casefold()


def split(text: str) -> list[str]:
    """The cells of one `cells:` line's text, whitespace collapsed, in order, each once."""
    out, seen = [], set()
    for part in SEP.split(text or ""):
        cell = " ".join(part.split())
        if norm(cell) and norm(cell) not in seen:
            seen.add(norm(cell))
            out.append(cell)
    return out


def is_name(cell: str) -> bool:
    """Whether one part of a `cells:` line is a cell name, not a description: at most NAME_WORDS words
    and NAME_CHARS chars, no brackets, no sentence end after several words, and a ':' only inside one
    space-free token (a device id such as 81:00.0)."""
    cell = " ".join(str(cell).split())
    if not cell or len(cell) > NAME_CHARS or len(cell.split()) > NAME_WORDS or re.search(r"[()\[\]{}<>]", cell):
        return False
    if " " in cell and cell[-1] in ".?!":
        return False
    return ":" not in cell or bool(TOKEN_COLON.fullmatch(cell))


def enumerable(text: str) -> bool:
    """Whether one `cells:` line's text (after the key) lists cell names only (is_name for every part)."""
    parts = [p for p in SEP.split(text or "") if p.strip()]
    return bool(parts) and all(is_name(p) for p in parts)


def ignored(spec) -> list[str]:
    """The `cells:` lines of `spec` that are not enumerable: they request no cells."""
    return [t.strip() for t in LINE.findall(str(spec or "")) if t.strip() and not enumerable(t)]


def problem(spec) -> str | None:
    """Why `spec` (a coordinator task_add or task_update spec) is refused for its `cells:` lines, or None."""
    bad = ignored(spec)
    if not bad:
        return None
    line = bad[0] if len(bad[0]) <= 120 else bad[0][:117] + "..."
    return (f"its `cells: {line}` line is a description, not a list of cells: list one short name per "
            f"measurement or test (e.g. one per chip id or test name, `cells: a, b, c`) and put the "
            f"description elsewhere in the spec")


def requested(spec) -> list[str]:
    """The cells `spec`'s enumerable `cells:` lines request, in order, each once; [] without such a line
    (a descriptive line, ignored(), requests none)."""
    return split(",".join(t for t in LINE.findall(str(spec or "")) if enumerable(t)))


def _valid(value) -> bool:
    """A dict cell's value counts as finished unless it is None, False, empty, an 'invalid...' string or
    `valid: false`; a number, 0 included, is a measurement."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and not value.strip().casefold().startswith("invalid")
    if isinstance(value, dict):
        return bool(value) and value.get("valid", True) is not False
    if isinstance(value, (list, tuple)):
        return bool(value)
    return True


def check(spec, result) -> dict | None:
    """None when `spec` requests no cells. Else {"requested": [...], "missing": [...]}, with "missing"
    None when the hand-off has no `cells` list or dict (a string is read as one `cells:` line)."""
    req = requested(spec)
    if not req:
        return None
    got = result.get(FIELD) if isinstance(result, dict) else None
    if isinstance(got, str):
        got = split(got)
    if not isinstance(got, (list, dict)):
        return {"requested": req, "missing": None}
    if isinstance(got, dict):
        done = {norm(k) for k, v in got.items() if _valid(v)}
    else:
        done = {norm(c) for c in got if isinstance(c, (str, int, float)) and not isinstance(c, bool)}
    return {"requested": req, "missing": [c for c in req if norm(c) not in done]}


def prefix(chk: dict | None) -> str:
    """The summary's prefix for check()'s answer: '' when every requested cell is finished."""
    if not chk or chk["missing"] == []:
        return ""
    m = len(chk["requested"])
    if chk["missing"] is None:
        return f"{UNREPORTED} {m} requested. "
    named = ", ".join(chk["missing"])
    named = named if len(named) <= NAMED_CHARS else named[:NAMED_CHARS - 3].rstrip(", ") + "..."
    return f"{PARTIAL} {m - len(chk['missing'])} of {m} cells (missing: {named}). "


def marked(summary: str) -> bool:
    """Whether `summary` already carries a prefix (a hand-off read twice is marked once)."""
    return str(summary or "").startswith((PARTIAL, UNREPORTED))


def followup(task: dict, missing: list[str]) -> dict:
    """The `continue:` follow-up for the cells done `task` left missing: its own `cells:` line lists
    only those, and its parent's spec follows without its `cells:` lines."""
    rest = LINE.sub("", str(task.get("spec") or "")).strip()
    rest = rest if len(rest) <= SPEC_CHARS else rest[:SPEC_CHARS] + " [cut]"
    spec = (f"Finish the requested cells #{task['id']} left without a valid result; the cells it finished "
            f"are done, so do not rerun them.\ncells: {', '.join(missing)}")
    if rest:
        spec += f"\n\n#{task['id']}'s spec, its `cells:` line replaced by the one above:\n{rest}"
    return {"title": f"continue: missing cells of #{task['id']} {task.get('title') or ''}".strip()[:200],
            "spec": spec}


def has_followup(fups: list) -> bool:
    """Whether the hand-off already proposes a `continue:` follow-up with its own `cells:` line."""
    return any(isinstance(f, dict) and re.match(r"(?i)^\s*continue\s*:", str(f.get("title") or ""))
               and requested(f.get("spec")) for f in fups or [])
