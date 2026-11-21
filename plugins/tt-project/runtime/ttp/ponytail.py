# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The ponytail lane and size gate, model-free (skills tt-ponytail and tt-ponytail-review).

A daemon-queued review of a code change (LABEL) also runs a separate ponytail review on a pinned model in a
fresh context: is this the smallest change that solves the task, and should any of it exist? Its verdict
goes, unedited, to FILE in the run dir. A labelled review hands off `done` only with a valid FILE
(hook.handoff_problem), passes only when it says pass and has no blocking finding (lane_problem; a `note`
finding never fails it), and its own `ttp push` lands only then (push._resolve_target). A change over a size threshold (added lines, git numstat, binaries and
delivery.push_exclude_paths left out) also needs a reason, a WHY line or the code task's splitgate no_split
hand-off (reason_text): review.ponytail_land_lines before landing (the push queue's approval measures each
approved head again, Daemon._ponytail_approval), review.ponytail_pr_lines (stricter: repos reached only by
pull request, another team's) before a draft PR."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

LABEL = "ponytail_lane:"     # ponytail_lane:<added lines at queue time, empty when unknown>: the review runs the lane
FILE = "ponytail.json"       # {"verdict": "pass"|"fail", "head": <sha>, "findings": [{"severity", "text"}], "thread": bool}
ENV = "TTP_PONYTAIL_LANE"    # 1: this run's hand-off needs FILE (hook.handoff_problem)
FAILED = "the ponytail lane failed it"   # lane_problem's prefix for a valid fail verdict
WHY = re.compile(r"(?im)^[ \t>*-]*why this size:[ \t]*\S")


def _review(cfg: dict) -> dict:
    return cfg.get("review") or {}


def _test_off() -> bool:
    # TTP_TEST_PONYTAIL: the test suite's fixture sets "off" so other tests see reviews and pushes unchanged.
    return os.environ.get("TTP_TEST_PONYTAIL") == "off"


def on(cfg: dict) -> bool:
    return bool(_review(cfg).get("ponytail_lane", True)) and not _test_off()


def limit(cfg: dict, pr: bool = False) -> int:
    """Added lines above which a change needs a WHY line and a passed lane: 0 turns the gate off."""
    return 0 if _test_off() else int(_review(cfg).get("ponytail_pr_lines" if pr else "ponytail_land_lines") or 0)


def labelled(task: dict | None) -> str | None:
    """The size a labelled review was queued with ('' when unknown), None when it runs no lane."""
    try:
        labels = json.loads((task or {}).get("labels") or "[]")
    except ValueError:
        return None
    return next((lb[len(LABEL):] for lb in labels if isinstance(lb, str) and lb.startswith(LABEL)), None) \
        if isinstance(labels, list) else None


def carried(*reviews: dict) -> list[str]:
    """A re-review's lane label: the first of `reviews` that ran the lane, [] when none did."""
    return next(([label(int(n) if n.isdigit() else None)] for r in reviews if (n := labelled(r)) is not None), [])


def label(added: int | None) -> str:
    return f"{LABEL}{'' if added is None else added}"


def added_lines(repo, base: str, head: str, exclude: list[str] = ()) -> int | None:
    """Lines `head` adds since its merge base with `base` (worktree.diff_lines), binaries and `exclude`
    globs left out; None when git cannot tell or it changes nothing measurable."""
    from .push import matches
    from .worktree import diff_lines
    try:
        per_file = diff_lines(None, [head], base=base, root=Path(repo), added=True)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None
    return None if per_file is None else sum(n for f, n in per_file.items()
                                              if n and not any(matches([f], g) for g in exclude))


def own_size(p, task: dict, branch: str, head: str, exclude: list[str] = ()) -> tuple[int | None, bool]:
    """(lines `head` of code task `task`'s branch `branch` adds since its own base (worktree.own_base, which
    measures a branch made from the push branch's history before a rewrite by its own commits), whether
    it has a change of its own: False when it adds no commit since that base, or its change already is on
    the push branch, as it stands or before a rewrite (landed.on_push: an ancestor, or each commit's
    equivalent) or as a squash (landed.equivalent))."""
    from . import landed, worktree
    base = worktree.own_base(p, task, branch)[0]
    added = added_lines(p.root, base, head, exclude)
    if worktree._git(p.root, "rev-list", "--count", f"{base}..{head}", check=False) == "0":
        return added, False
    try:
        tip = landed._commit(p.root, landed._push_ref(p, p.root))
    except ValueError:
        return added, True
    bump = (p.config().get("delivery") or {}).get("version_bump")
    on = bool(tip) and bool(landed.on_push(p, p.root, [head], tip) or landed.equivalent(p, branch, bump))
    return added, not on


def read(run_dir) -> dict | None:
    try:
        v = json.loads((Path(run_dir) / FILE).read_text())
    except (OSError, ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def lane_problem(run_dir, head: str = "") -> str:
    """Why the lane's verdict in `run_dir` does not let the change through ('' when it passed `head`)."""
    v = read(run_dir) if run_dir else None
    if v is None:
        return f"the ponytail lane recorded no verdict ({FILE} in the run dir)"
    verdict = str(v.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "fail"):
        return f"{FILE} verdict {v.get('verdict')!r} is neither pass nor fail"
    if findings(v) or verdict == "fail" and not findings(v, note=True):   # notes alone never fail the lane
        return f"{FAILED}: " + ("; ".join(findings(v)) or "no findings given")[:600]
    seen = str(v.get("head") or "")
    if head and not (seen and (head.startswith(seen) or seen.startswith(head)) and len(seen) >= 7):
        return f"the ponytail lane passed {seen[:12] or 'no head'}, not {head[:12]}"
    return ""


def findings(v: dict, note: bool = False) -> list[str]:
    """The verdict's finding texts: the blocking ones (severity 'block', or none given: a bare string too),
    or with `note` the 'note' ones (a small leftover that can wait), which never fail the lane."""
    out = []
    for x in v.get("findings") if isinstance(v.get("findings"), list) else []:
        sev, text = (str(x.get("severity") or "").strip().lower(), x.get("text")) if isinstance(x, dict) else ("", x)
        if str(text or "").strip() and (sev == "note") == note:
            out.append(str(text)[:300])
    return out


def size_problem(cfg: dict, added: int | None, text: str, run_dir, head: str = "", pr: bool = False) -> str:
    """Why a change of `added` lines may not land (or, with `pr`, open as a draft PR): over the limit
    with no WHY line in `text` (its spec and hand-off) or no passed lane in `run_dir`. '' otherwise."""
    cap = limit(cfg, pr)
    if not cap or added is None or added <= cap:
        return ""
    key = "review.ponytail_pr_lines" if pr else "review.ponytail_land_lines"
    if not WHY.search(text or ""):
        return (f"it adds {added} lines, over {key} ({cap}): its spec or hand-off needs the reason on its own "
                f"line, starting with `why this size:` and followed by the reason (the phrase mid-line is not "
                f"read), and a passed ponytail lane (skill tt-ponytail-review, verdict in "
                f"$TTP_RUN_DIR/{FILE}); else make it smaller")
    if why := lane_problem(run_dir, head):
        return f"it adds {added} lines, over {key} ({cap}), and {why}"
    return ""


def skill_file() -> str:
    """The ponytail review skill's own file, resolved at run time where this release keeps its
    skills (cli.skill_dirs: the plugin's copy first, then the installed one)."""
    from .cli import skill_dirs
    for d in skill_dirs():
        if (d / "tt-ponytail-review" / "SKILL.md").is_file():
            return str(d / "tt-ponytail-review" / "SKILL.md")
    return "skills/tt-ponytail-review/SKILL.md"


def spec_line(cfg: dict, added: int | None) -> str:
    """The review spec's lane step; with `added` over the landing limit, the size rule too."""
    model = _review(cfg).get("ponytail_model") or "opus"
    line = (f"Ponytail lane (review.ponytail_lane): beside correctness, run a separate ponytail review. Start one "
            f"fresh-context subagent pinned to model `{model}`, given only the change's spec and its diff, and "
            f"have it read skill `tt-ponytail-review` at `{skill_file()}` and apply it. It fails the change on any "
            f"`block` finding (code that should not exist, a copied or vendored tree unless the spec has a line "
            f"`vendored: <user msg>`); a `note` (a small leftover that can wait) does not. Write its "
            f"verdict unedited to `$TTP_RUN_DIR/{FILE}` as {{\"verdict\": \"pass\" or \"fail\", \"head\": "
            f"\"<full sha reviewed>\", \"findings\": [{{\"severity\": \"block\" or \"note\", \"text\": "
            f"\"...\"}}], \"thread\": true}} before any `ttp push` or push list; "
            f"with no subagent tool, run the lane yourself last and set \"thread\": false. A fail fails this "
            f"review (its findings become the fix follow-ups); landing, the push queue and a draft PR's next "
            f"step need both verdicts to pass.")
    if added is not None and (cap := limit(cfg)) and added > cap:
        line += (f" Size: it adds {added} lines, over review.ponytail_land_lines ({cap}): it passes only with a "
                 f"`why this size:` line in its spec or hand-off.")
    return line


def reason_text(review: dict, result: dict, reviewed: list[dict] = ()) -> str:
    """Where a change's size reason may be: the review's spec and hand-off, the reviewed tasks' specs and
    hand-offs, and their splitgate no_split hand-off field (read as a WHY line)."""
    from . import splitgate
    from .db import load_result
    results = [load_result(t.get("result")) for t in reviewed]
    why = splitgate.reason(*results)
    return "\n".join([str(review.get("spec") or ""), str((result or {}).get("summary") or ""),
                      *(str(t.get("spec") or "") for t in reviewed), *(str(r.get("summary") or "") for r in results),
                      f"why this size: {why}" if why else ""])


def gate(cfg: dict, task: dict, run_dir, result: dict, reviewed: list[dict] = ()) -> tuple[str, list[str]]:
    """For labelled review `task` handing off done: (why it fails, what the change must fix), ('', []) when
    it passes. A missing or unreadable verdict fails with nothing to fix in the change."""
    size = labelled(task)
    if size is None:
        return "", []
    v = read(run_dir) or {}
    if why := lane_problem(run_dir):
        return why, findings(v)
    added = int(size) if size.isdigit() else None
    why = size_problem(cfg, added, reason_text(task, result, reviewed), run_dir)
    return why, [why] if why else []


def required() -> bool:
    return os.environ.get(ENV) == "1"
