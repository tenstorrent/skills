"""The spec a worker reads, with a long tail of appended `## Update` blocks folded (user msg #2271).

A task_update appends its text after a `## Update` line, so a task steered many times carries every
update whole into each run's first call. Past SPEC_FOLD_CHARS and SPEC_FOLD_KEEP updates, the worker's
prompt keeps the original spec, the latest updates whole (at least the latest one, more while they fit
SPEC_FOLD_CHARS, at most SPEC_FOLD_KEEP), every `## Obligation` block the harness appended (in place,
whole), and from each older update every line that binds: safety lines (a Restrictions or guards
block, never, no_push and the like), lines quoting or citing a user's or owner's words or orders
("user msg #N", "owner's agent", "relayer"), urgent lines, must-lines and the
`pr_branch:`/`start_when:` style fields. `retire:` effects are already applied to the stored spec
(coordinator.spec_edits), so a "Retired:" notice adds nothing. The older updates go whole to a file
the worker reads on demand. The task's stored spec is never changed: only its prompt."""
from __future__ import annotations

import re
from pathlib import Path

SPEC_FOLD_CHARS = 16000
SPEC_FOLD_KEEP = 3
FOLD_HEAD = "## Earlier updates, folded"
_SECTION = re.compile(r"(?m)^(?=#{2,3} (?:Update|Obligation)\b)")
_UPDATE = re.compile(r"^## Update\b")
_WHOLE = re.compile(r"\b(?:user|owner)'s\s+words?\b|verbatim|\b(?:user|owner)(?:'s)?\s+#\d+|"
                    r"\b(?:user|owner)\s+(?:said|asked|ordered)\b|"
                    r"\b(?:user|owner)(?:'s)?\s+(?:msg|message|order)s?\b|\bowner's\s+agent\b|"
                    r"\brelayer\b|"
                    r"^\s*[-*]?\s*(?:pr_branch|no_push|start_when|retry_when|depends_on):", re.I)
_KEEP = re.compile(r"\burgent\b|\bmust\b|\bnever\b|\bdo not\b|\bdon't\b|\bonly\b", re.I)


def _sections(spec: str) -> list[str]:
    return [x for x in _SECTION.split(spec) if x]


def _binding(text: str) -> list[str]:
    """What of one folded update a worker must still read (see the module doc): a safety line, a line
    quoting a user's or owner's words and a field line whole; of any other line, only its sentences that
    bind (never, must, urgent...), so a long note keeps its one rule and drops the rest to the file."""
    from .coordinator import RETIRED_LIVE, _safety_lines
    lines = text.split("\n")
    safety = _safety_lines(text)
    out = []
    for i, ln in enumerate(lines[1:], 1):
        if not ln.strip() or ln.strip() == RETIRED_LIVE:
            continue
        if i in safety or _WHOLE.search(ln):
            out.append(ln.rstrip())
        elif _KEEP.search(ln):
            out.append(" ".join(x for x in re.split(r"(?<=[.!?])\s+", ln.strip()) if _KEEP.search(x)))
    return out


def fold(spec: str, path: str | None = None, budget: int = SPEC_FOLD_CHARS,
         keep: int = SPEC_FOLD_KEEP) -> tuple[str, str]:
    """(the spec as the worker reads it, the older updates' whole text, "" when nothing is folded).
    `path`: where the worker finds that text, named in the folded section's head."""
    if len(spec) <= budget:
        return spec, ""
    secs = _sections(spec)
    ups = [i for i, s in enumerate(secs) if i and _UPDATE.match(s)]
    if len(ups) <= 1:
        return spec, ""
    whole = ups[-1:]   # the latest always; then the next older while they fit and keep allows
    for i in reversed(ups[:-1]):
        if len(whole) >= keep or sum(len(secs[j]) for j in whole) + len(secs[i]) > budget // 2:
            break
        whole.insert(0, i)
    folded = [i for i in ups if i not in whole]
    if not folded:
        return spec, ""
    shown = "".join(s for j, s in enumerate(secs) if j not in folded)
    seen: set[str] = set()
    kept: list[str] = []
    for i in folded:   # a line the shown text or an earlier kept one already says is said once
        for ln in _binding(secs[i]):
            if ln.strip() not in seen and ln.strip() not in shown:
                seen.add(ln.strip())
                kept.append(ln)
    where = f" in {path}" if path else " in the task's record (`ttp task <project> show <id>`)"
    head = (f"{FOLD_HEAD} ({len(folded)} older updates; each whole{where}, read it when a line below needs its "
            "context). Their binding lines, in order (the user's words, restrictions, urgent and must lines; "
            "retire: effects already applied above):\n")
    out, done = [], False
    for i, s in enumerate(secs):
        if i in folded:
            if not done:
                out.append(head + ("\n".join(kept) if kept else "(none)") + "\n\n")
                done = True
            continue
        out.append(s)
    return "".join(out).rstrip("\n") + "\n", "\n\n".join(secs[i].strip("\n") for i in folded) + "\n"


def for_prompt(state: Path, task: dict) -> str:
    """The task's spec for its worker's prompt: folded, the older updates written to
    <state>/spec-updates/<task id>.md (rewritten each time, never in place)."""
    from .project import durable_write
    spec = task["spec"] or task["title"]
    path = Path(state) / "spec-updates" / f"{task['id']}.md"
    shown, older = fold(spec, str(path))
    if older:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            durable_write(path, f"# Older spec updates of task #{task['id']}, whole, oldest first\n\n" + older,
                          sync=False)
        except OSError:   # an unwritable file must not stop the prompt: the record keeps them
            return fold(spec)[0]
    return shown
