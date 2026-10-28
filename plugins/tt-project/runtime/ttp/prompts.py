# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Worker prompts: harness rules + charter (restrictions are binding) + memory + the task.
Templates live in the project's own harness (prompts/*.md), so each project can tune them."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import budget as bud
from . import chaincap, conflictfix, overlap, replymsg, settinghold, settled, taskfacts, timefmt
from .db import continues_id, load_result
from .hook import unread_update
from .project import rebase_replace
from .project import (CORRECTIONS_HEADER, OWN_FLAG, WORKER_MEMORY_CHARS, WORKER_PINNED_MIN_CHARS, WORKER_RANKED_MIN_CHARS,
                      Project, code_tasks_may_push, own_push, push_allowed, push_queue_on, worker_memory,
                      WORKER_POLICY_MIN_CHARS)
from .worktree import carried_branch, gets_worktree, own_worktree, project_venv, rebased_branch, recovery_note


def _read(p: Project, name: str, on: set[str] | None = None) -> str:
    """Harness prompt `name`, without its fences; `on`: also without the blocks of features not in it."""
    f = p.harness / "prompts" / name
    if not f.exists():
        return ""
    return strip_markers(f.read_text()) if on is None else feature_blocks(f.read_text(), on)


def strip_markers(text: str) -> str:
    """`text` without its `<!-- ttp:... -->` / `<!-- /ttp:... -->` lines. They fence a block (such as
    kind-review.md's result rule) so a project's own wording of it merges as one unit on upgrades;
    the model never needs them."""
    return "".join(line for line in text.splitlines(keepends=True)
                   if not (line.startswith(("<!-- ttp:", "<!-- /ttp:")) and line.rstrip().endswith("-->")))


_IF_RE = re.compile(r"^<!-- (/?)ttp:if (!?)([a-z_]+) -->\s*$")


def feature_blocks(text: str, on: set[str]) -> str:
    """`text` without each `<!-- ttp:if X -->` ... `<!-- /ttp:if X -->` block whose feature X is not in
    `on` (`ttp:if !X` keeps its block only while X is off), then without its marker lines. The
    coordinator's role prompt fences its rules for features most projects never turn on this way."""
    out, skip = [], []
    for line in text.splitlines(keepends=True):
        m = _IF_RE.match(line)
        if m and not m.group(1):
            skip.append((m.group(3) in on) == bool(m.group(2)))
        elif m:
            if skip:
                skip.pop()
        elif not any(skip):
            out.append(line)
    return strip_markers("".join(out))


def coordinator_features(p: Project) -> set[str]:
    """The features whose fenced rules the coordinator's role prompt carries (feature_blocks)."""
    from . import forbidden, machines, obligations, push
    cfg = p.config()
    d = cfg.get("delivery") or {}
    on = set()
    if bool(str(d.get("push_branch") or "").strip()) and push_allowed(d):
        on.add("push_branch")
    # checkgaps.wait opens a check:<sig> gap for any delivery.push_checks, push branch or not.
    if push.all_checks(d):
        on.add("push_checks")
    if push_queue_on(cfg):
        on.add("push_queue")
    if code_tasks_may_push(cfg):
        on.add("land")
    mode = own_push(cfg)
    on |= {"own_push"} if mode != "off" else set()
    on |= {"rebase_lease"} if rebase_replace(cfg) == "lease" else set()
    # Unset reads as existing_only, but its section costs every coordinator turn: the own_push one names it.
    on |= {"own_push_limited"} if mode != "on" and d.get("own_push") is not None else set()
    if d.get("draft_prs", True) is not False:
        on.add("draft_prs")
    if d.get("drafts_during_freeze", True) is not False:
        on.add("drafts_during_freeze")
    if (cfg.get("notify") or {}).get("slack"):
        on.add("slack")
    if (cfg.get("upstream") or {}).get("ingest"):
        on.add("upstream_ingest")
    try:
        known = machines.load()
    except Exception:
        known = {}
    if known:
        on.add("machines")
    # Shared-cluster rules: a machine tagged as one, or a cluster in the charter's Resources (its
    # template's standing Policies line about clusters says nothing about this project's own).
    charter = p.charter_path.read_text() if p.charter_path.exists() else ""
    resources = re.search(r"(?ms)^## Resources\b.*?(?=^## |\Z)", charter)
    tags = " ".join(str(m.get("tags") or "") for m in known.values())
    if re.search(r"\b(slurm|cluster)", f"{tags} {resources.group(0) if resources else ''}", re.I):
        on.add("clusters")
    if change_windows(charter):
        on.add("change_windows")
    # Pointer rule for projects that keep their data out of git: on once any forbidden-data pattern is declared.
    mem = cfg.get("memory") or {}
    if mem.get("forbidden_patterns") or mem.get("forbidden_patterns_file") or any(
            forbidden.is_section(h) for h, _ in charter_sections(charter)):
        on.add("forbidden_data")
    try:
        if any(o.get("receipt") is None and o.get("dropped") is None for o in obligations.items(p.db)):
            on.add("obligations")
    except Exception:
        pass
    return on


def change_windows(charter: str) -> str:
    """The charter's `- Change windows:` Policies line, or "" while it says `none` (the template's
    default) or is absent: a team that works in change windows lists them there."""
    policies = re.search(r"(?ms)^## Policies\b.*?(?=^## |\Z)", charter)
    m = re.search(r"(?mi)^- Change windows:[ \t]*(.*)$", policies.group(0) if policies else "")
    value = m.group(1).strip() if m else ""
    return "" if re.match(r"(?i)none\b", value) else value


def coordinator_role(p: Project) -> str:
    """The coordinator's role prompt (prompts/coordinator.md) with the rules for this project's
    features only."""
    return feature_blocks((p.harness / "prompts" / "coordinator.md").read_text(), coordinator_features(p))


# kind-review.md's two delivery sections; a review's prompt carries only the one its project uses.
REVIEW_PUSH_SECTIONS = ("## Pushing a reviewed change", "## Approving into the push queue")


OWN_SECTION = "## Publishing onto its own PR branch"   # kind-review.md's `ttp push --own` section


def _sections(text: str, cfg: dict) -> str:
    """A kind prompt without the push section that does not apply: the plain push's or the queue's,
    the `ttp push --own` advice while own pushes are off (delivery.own_push), and its upstream note
    rules while this project does not read the upstream inbox (upstream.ingest)."""
    text = feature_blocks(text, {"upstream_ingest"} if (cfg.get("upstream") or {}).get("ingest") else set())
    text = drop_section(text, REVIEW_PUSH_SECTIONS[0 if push_queue_on(cfg) else 1])
    return own_off(drop_section(text, OWN_SECTION)) if own_push(cfg) == "off" else text


# kind-code.md's advice for a push refused as based on another branch, and its stand-in with own pushes off.
OWN_REFUSAL = re.compile(r"else publish it with\s+`ttp push --own --detach`\.")
OWN_REFUSAL_OFF = "else hand off `blocked` with its message (own pushes are off: delivery.own_push)."
NEGATED_OWN = re.compile(r"\b(?:not|never|no)\b[^.;]*--own", re.I)
SENTENCE_END = re.compile(r"(?<=[.;!?])(\s+)")


def _asks_own(sentence: str) -> bool:
    return bool(OWN_FLAG.search(sentence)) and not NEGATED_OWN.search(sentence)


def own_off(text: str) -> str:
    """A kind prompt with own pushes off: the refusal advice says hand off `blocked`, a list item whose
    first sentence tells the worker to run `ttp push --own` (kind-code.md's PR publishing bullet) is
    dropped, and any other sentence asking for one is cut, so no worker spends a run on a push the
    harness refuses. A warning against it (do not, never) stays."""
    out, item = [], []

    def flush():
        x = "".join(item)
        item.clear()
        if not OWN_FLAG.search(x):
            return out.append(x)
        parts = SENTENCE_END.split(x)   # sentence, separator, sentence, ...
        if _asks_own(parts[0]):
            return None
        kept = parts[0] + "".join(sep + sen for sep, sen in zip(parts[1::2], parts[2::2]) if not _asks_own(sen))
        out.append(kept.rstrip() + "\n" if x.endswith("\n") else kept.rstrip())
    for line in OWN_REFUSAL.sub(OWN_REFUSAL_OFF, text).splitlines(keepends=True):
        if not (item and item[0].startswith("- ") and line[:1] in (" ", "\t")):
            flush()
        item.append(line)
    flush()
    return "".join(out)


def drop_section(text: str, heading: str) -> str:
    """`text` without the `## ` section whose heading line starts with `heading`."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            skip = line.startswith(heading)
        if not skip:
            out.append(line)
    return "".join(out)


def charter_restrictions(charter: str) -> str:
    """Every `## Restrictions ...` section of the charter, concatenated.

    Workers start with no memory of the charter, so these have to be stated up front
    rather than only buried in the full charter further down the prompt."""
    out, keep = [], False
    for line in charter.splitlines():
        if line.startswith("## "):
            keep = line[3:].strip().lower().startswith("restriction")
            continue
        s = line.strip()
        if keep and s and not (s.startswith("(") and s.endswith(")")):  # skip "(none stated yet)"
            out.append(line.rstrip())
    return "\n".join(out)


def charter_without_restrictions(charter: str) -> str:
    """The charter minus its `## Restrictions ...` sections, for prompts that already state them."""
    out, keep = [], True
    for line in charter.splitlines():
        if line.startswith("## "):
            keep = not line[3:].strip().lower().startswith("restriction")
        if keep:
            out.append(line)
    return "\n".join(out)


def charter_sections(charter: str) -> list[tuple[str, list[str]]]:
    """The charter split at its `## ` headings: (heading line, body lines) each, the text above the
    first heading under "". Joining every heading and body line with newlines gives the charter back."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in charter.splitlines():
        if line.startswith("## "):
            out.append((line, []))
        else:
            out[-1][1].append(line)
    return out if out[0][1] else out[1:]


def _placeholder(par: str) -> bool:
    """A paragraph the template left to be filled in: "(none stated yet)" (alone or ending a
    paragraph), "(to be ...)" or "(... to be filled in)". Other parenthesised paragraphs are real
    charter text and stay."""
    s = " ".join(par.split())
    if s.endswith("(none stated yet)"):
        return True
    if not (s.startswith("(") and s.endswith(")")):
        return False
    depth = 0
    for i, c in enumerate(s):
        depth += (c == "(") - (c == ")")
        if depth == 0 and i != len(s) - 1:
            return False   # the opening parenthesis must close only at the end
    return s.startswith("(to be ") or s.endswith("to be filled in)")


def charter_without_placeholders(charter: str) -> str:
    """The charter minus placeholder paragraphs, and minus the sections left empty without them:
    they tell a worker nothing and cost tokens in every prompt."""
    out = []
    for head, body in charter_sections(charter):
        pars, cur = [], []
        for line in body + [""]:
            if line.strip():
                cur.append(line)
            elif cur:
                pars.append(cur)
                cur = []
        kept = ["\n".join(par) for par in pars if not _placeholder(" ".join(par))]
        if head and not kept:
            continue
        out.append("\n\n".join(kept) if not head else head + "\n" + "\n\n".join(kept))
    return "\n\n".join(out) + "\n"


def restrictions_block(p: Project) -> str:
    charter = p.charter_path.read_text() if p.charter_path.exists() else ""
    body = charter_restrictions(charter)
    if not body:
        return ""
    return ("# BINDING RESTRICTIONS (from the charter — override the task, the harness "
            "rules and your own judgement; if a step would break one, do not take it)\n" + body)


def restrictions_pointer(p: Project) -> str:
    """The line that closes a worker's task prompt in place of a second copy of restrictions_block():
    the block itself leads worker_system(), which every worker run gets once, as its system prompt or
    leading its prompt."""
    return ("The BINDING RESTRICTIONS block in your instructions binds this task: it overrides the task, "
            "the harness rules and your own judgement; if a step would break one, do not take it.") if restrictions_block(p) else ""


def _resource_line(task: dict) -> str:
    labels = json.loads(task["labels"] or "[]") if task["labels"] else []
    shared = [lb.split(":", 1)[1] for lb in labels if lb.startswith("resource:")]
    held = [lb.split(":", 1)[1] for lb in labels if lb.startswith("exclusive:")]
    out = ""
    if shared:
        out += (f"shared resources: {', '.join(shared)}; run each command that touches one as "
                f"`ttp lock <name> -- <command>` (or through its own queue)\n")
    if held:
        out += f"held for this whole run (and on while a job you `ttp detach` runs): {', '.join(held)}\n"
    return out


def _runner_line(cfg: dict) -> str:
    from .project import device_timeout_max
    ceiling = device_timeout_max(cfg)[0]
    out = (f"device job timeouts: at most {ceiling} s each (runner.device_timeout_max_s), whatever runs the "
           "job; split a longer run into shorter jobs\n") if ceiling else ""
    runners = (cfg.get("device") or {}).get("runners") or {}
    if not isinstance(runners, dict) or not runners:
        return out
    names = ", ".join(f"{n} (on {(r or {}).get('host') or 'this machine'})" for n, r in sorted(runners.items()))
    return (f"device runners: {names}; queue every device job with `ttp devq submit`, never a detached "
            "driver of your own\n") + out


def _hostmap_line(cfg: dict) -> str:
    from .hostmap import table
    tab = table(cfg)
    if not tab:
        return ""
    return ("resource hosts (probe with `ttp ssh-preflight @<resource>`, never the host): "
            + ", ".join(f"@{n} (on {h})" for n, h in sorted(tab.items())) + "\n")


# A charter item's own tag says who it is for; untagged, an item is for workers when it speaks of
# their work and names no coordinator duty.
_AUDIENCE_TAG = re.compile(r"\[(workers?|coordinator)\]", re.I)
_WORK_TERMS = re.compile(r"\b(workers?|branch(es)?|push(es|ed|ing)?|pull requests?|PRs?|commits?|tests?|"
                         r"changesets?|versions?|merges?|skills?|builds?|jobs?|nodes?)\b", re.I)


def _items(body: list[str]) -> list[str]:
    """A section's body as its items: each top-level bullet with its continuation lines, or each
    paragraph."""
    out: list[list[str]] = []
    cur: list[str] = []
    for line in body:
        if not line.strip() or re.match(r"(?:[-*+]|\d+[.)])\s", line):
            if cur:
                out.append(cur)
            cur = [line] if line.strip() else []
        else:
            cur.append(line)
    if cur:
        out.append(cur)
    return ["\n".join(x) for x in out]


def for_workers(item: str) -> bool:
    """Whether a policy (or other charter item) belongs in a worker's prompt."""
    tag = _AUDIENCE_TAG.search(item)
    if tag:
        return tag[1].lower().startswith("worker")
    return bool(_WORK_TERMS.search(item)) and not re.search(r"\bcoordinator\b", item, re.I)


_CHARTER_HEAD = ("# CHARTER (for workers: goals, resources and the policies for workers; its restrictions are "
                 "the binding block above. Full charter{more}: {path})\n")
_MEMORY_HEAD = "# PROJECT MEMORY (entries for workers; all {n} entries: {path})\n"


def _worker_heads(p: Project, dropped: int, n: int) -> tuple[str, str]:
    more = f"; {dropped} more items for the coordinator" if dropped else ""
    return _CHARTER_HEAD.format(more=more, path=p.charter_path), _MEMORY_HEAD.format(n=n, path=p.memory_index)


# Where a worker finds the memory pinned for it that its prompt has no room for: a pointer, not a fault.
_PINNED_MORE = "({n} more entries for workers are in {path}/: read those your task touches)"


def _norm(text: str) -> str:
    """`text` lower-cased with its list markers dropped and its whitespace collapsed."""
    return " ".join(re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", "", text).lower().split())


def restated_in(block: str):
    """A test of whether a memory entry's text is already stated by `block` (the charter's
    restrictions): it is one of its items, or whole sentences of one, or each of its sentences is
    whole sentences of some item, ignoring case, list markers and spacing. "Never push." is not
    restated by "Never push to main.": a match ends where a sentence does."""
    items = [_norm(x) for x in _items(block.splitlines())]

    def test(text: str) -> bool:
        t = _norm(text)
        if one(t):
            return True
        parts = [s for s in re.split(r"(?<=[.;!?])\s+", t) if s]
        return len(parts) > 1 and all(one(s) for s in parts)

    def one(t: str) -> bool:
        for item in items if t else ():
            i = item.find(t)
            while i >= 0:
                j = i + len(t)
                if (i == 0 or item[:i].rstrip()[-1:] in ".;!?") and (
                        j == len(item) or t[-1] in ".;!?" or item[j] in ".;!?"):
                    return True
                i = item.find(t, i + 1)
        return False
    return test


def worker_context(p: Project, task: dict | None = None,
                   entries: list[dict] | None = None) -> tuple[str, list[dict], dict]:
    """A worker's digest of the charter and memory: the Goals and Resources whole (the Brief only when
    no Goals are stated) and the policies and other charter items meant for workers (see
    for_workers) within what those leave of WORKER_MEMORY_CHARS, never less than
    WORKER_POLICY_MIN_CHARS; then memory pinned for workers (see worker_memory) in
    what is left of it, never less than WORKER_PINNED_MIN_CHARS, and the entries that share words with
    `task`, those that speak of workers and the newest facts while all still fit WORKER_MEMORY_CHARS,
    or WORKER_RANKED_MIN_CHARS past the pinned ones if that is more (see worker_memory_select).
    Memory never pushes out a policy. Memory entries that restate the charter's restrictions are left
    out: the prompt states those whole in their own block. Pinned entries past their budget are
    counted in `pinned_dropped` (worker_system says where to read them). `entries`: memory entries in
    place of memory/'s (see Project.worker_memory_select). Returns (charter text, memory entries, usage)."""
    raw = p.charter_path.read_text() if p.charter_path.exists() else ""
    secs = [(h, b) for h, b in charter_sections(charter_without_placeholders(raw))
            if h and not h[3:].strip().lower().startswith("restriction")]
    name = lambda h: h[3:].strip().lower()
    goals = any(name(h).startswith("goal") for h, _ in secs)
    whole = lambda h: name(h).startswith(("goal", "resource")) or (not goals and name(h).startswith("brief"))
    heads = sum(len(x) + 2 for x in _worker_heads(p, 999, 99999))   # their longest
    used = heads + sum(len(h) + len("\n".join(b).strip()) + 3 for h, b in secs if whole(h))
    out, dropped = [], 0
    cap = max(WORKER_MEMORY_CHARS, used + WORKER_POLICY_MIN_CHARS)   # long Goals never squeeze the policies out
    for h, b in secs:
        if whole(h):
            out.append(h + "\n" + "\n".join(b).strip())
            continue
        if name(h).startswith("brief"):
            continue
        kept = []
        used += len(h) + 3
        for item in _items(b):
            sep = 2 if kept else 0   # the blank line before every kept item but the first
            if for_workers(item) and used + sep + len(item) <= cap:
                kept.append(item)
                used += sep + len(item)
            else:
                dropped += 1
        if kept:
            out.append(h + "\n" + "\n\n".join(kept))
        else:
            used -= len(h) + 3
    text = "\n\n".join(out)
    rest = max(0, WORKER_MEMORY_CHARS - heads - len(text))
    room = max(rest, WORKER_PINNED_MIN_CHARS)   # for pinned entries
    restated = restated_in(charter_restrictions(raw))
    mem, use = p.worker_memory_select(rest, task, room, restated, entries)
    if use["pinned_dropped"]:   # make room for the line saying so
        room -= len(_PINNED_MORE.format(n=999, path=p.memory_dir)) + 1
        mem, use = p.worker_memory_select(min(rest, room), task, room, restated, entries)
    elif use["pinned_chars"] + WORKER_RANKED_MIN_CHARS > rest:   # every pinned one shown: the others' floor
        mem, use = p.worker_memory_select(use["pinned_chars"] + WORKER_RANKED_MIN_CHARS, task, room, restated, entries)
    return text, mem, dict(use, charter_chars=heads + len(text), charter_dropped=dropped, limit=WORKER_MEMORY_CHARS,
                           memory_limit=room)


# Task kinds whose workers never hand off `waiting` on a job of their own, so their prompt leaves out
# the detail of detached drivers, `on_pass_cmd` and wakes; the `waiting` basics stay.
NO_WAIT_KINDS = ("question", "plan")
# Task kinds that may publish a branch or a pull request.
PR_KINDS = ("code", "review")


def _named(alias: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w.-]){re.escape(alias)}(?![\w-])", text))


def worker_features(p: Project, task: dict | None = None) -> set[str]:
    """The features whose fenced rules worker.md carries for `task` (feature_blocks): its kind's
    (`harness`, `prs`, `wait`), and the project's or task's devices, device runners (`devq`), jobs on
    other machines (`remote`) and shared clusters (`clusters`), `cells` for a spec with a `cells:` line
    (cells.requested), and `corrections` while the user has corrected a fact (worker_corrections). No
    task: a `work` task's."""
    from . import machines
    cfg = p.config()
    kind = (task or {}).get("kind") or "work"
    on = {kind} if kind == "harness" else set()
    if kind in PR_KINDS:
        on.add("prs")
    if kind not in NO_WAIT_KINDS:
        on.add("wait")
    # Each check that cannot be read turns on the rules it could not rule out: a missing rule costs
    # more than a few hundred extra characters.
    unsure = set()
    try:
        known = machines.load()
    except Exception:
        known, unsure = {}, {"device", "remote", "clusters"}
    charter = p.charter_path.read_text() if p.charter_path.exists() else ""
    resources = " ".join(re.findall(r"(?ms)^## Resources\b.*?(?=^## |\Z)", charter))
    task_text = " ".join(str((task or {}).get(k) or "") for k in ("title", "spec", "labels"))
    # Machines this project may use (the charter names them) and the ones this task names.
    used = {a: m for a, m in known.items() if _named(a, charter) or _named(a, task_text)}
    tags = {}
    for a, m in used.items():
        try:
            tags[a] = machines.tag_list(m.get("tags"))
        except Exception:
            unsure |= {"device", "remote", "clusters"}
    device = cfg.get("device") if isinstance(cfg.get("device"), dict) else {}
    runners = device.get("runners") or {}
    if not isinstance(runners, dict):
        runners, unsure = {}, unsure | {"devq", "device", "remote"}
    if runners:
        on.add("devq")
    try:
        here = machines.device_host(known)
    except Exception:
        here, unsure = False, unsure | {"device"}
    from .effort import device_task
    from .locks import device_locks
    holds = bool(task) and device_task(task)
    if runners or here or device_locks(cfg) or holds or any("device" in t for t in tags.values()):
        on.add("device")
    # A job on another machine: the project or the task names one, or a runner is remote.
    if used or any((r or {}).get("host") for r in runners.values() if isinstance(r, dict)):
        on.add("remote")
    if re.search(r"\b(slurm|cluster)", resources, re.I) or any(
            {"slurm", "cluster"} & set(t) for t in tags.values()):
        on.add("clusters")
    from .hostmap import table
    if table(cfg):
        on.add("hostmap")
    if worker_corrections(p):   # the rule that reads them comes with the list
        on.add("corrections")
    from .cells import requested
    if requested((task or {}).get("spec")):   # the hand-off's `cells` field, only for a spec that asks for it
        on.add("cells")
    return on | unsure


def worker_corrections(p: Project) -> list[dict]:
    """The user's corrections a worker's prompt lists ahead of its memory (Project.user_corrections)."""
    return [e for e in p.user_corrections() if worker_memory(e)]


def worker_system(p: Project, task: dict | None = None) -> str:
    """The part of a worker's prompt shared by every task of the same shape (kind, devices, machines
    it names) until the charter or memory changes: sent as the system prompt where the agent allows
    it, so the provider caches it across the project's workers. Only worker_features() and the memory
    entries' choice may read the task; the memory comes last, so on a project whose memory does not fit
    whole only it differs between tasks and everything before it still caches."""
    parts = [restrictions_block(p), _read(p, "worker.md", worker_features(p, task))]
    # The one copy of the restrictions (the task prompt only points back at it); a second inside the
    # charter only costs tokens.
    # The rest of the charter and memory is mostly the coordinator's: workers get their digest of it.
    charter, mem, use = worker_context(p, task)
    # The user's corrections come before the rest of memory, outside its budget (Project.user_corrections).
    fixed = worker_corrections(p)
    p.note_memory_shown([e["name"] for e in fixed + mem])
    head, mem_head = _worker_heads(p, use["charter_dropped"], len(p._memory_entries()))
    parts.append(head + (charter or "(none)"))
    if fixed:
        parts.append(f"# {CORRECTIONS_HEADER.upper()}\n" + "\n".join(e["line"] for e in fixed))
    more = [_PINNED_MORE.format(n=use["pinned_dropped"], path=p.memory_dir)] if use["pinned_dropped"] else []
    if mem or more:
        parts.append(mem_head + "\n".join([e["line"] for e in mem] + more))
    return "\n\n".join(x for x in parts if x.strip())


def _zone_line(p: Project, now: float | None = None) -> str:
    """The project's home zone and the time there now, for the task header."""
    tz, now = timefmt.home(p), time.time() if now is None else now
    return (f"home time zone: {tz}, now {timefmt.long_utc(now, tz)}; today is {timefmt.today(now, tz)} there: "
            f"write dates and times for the user in it, over any other 'today' line\n")


def _reboot_note(reboot, tz: str | None = None) -> str:
    """What a run lost to a host reboot, or a wait from before one, must know before it redoes work."""
    if not isinstance(reboot, dict):
        return ""
    at = reboot.get("at")
    when = (timefmt.long(at, tz) if tz else time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(at))) \
        if isinstance(at, (int, float)) else "recently"
    planned = f" It was a planned host cycle: {reboot['planned']}." if reboot.get("planned") else ""
    out = (f"The host rebooted (booted {when}) since the last run; that does not count as an attempt.{planned} "
           "Detached jobs, /tmp files and device state from before the reboot are gone. Check `git status` "
           "and the job logs before redoing work.\n")
    notes = [str(x) for x in reboot.get("notes") or []]
    if notes:
        out += "The last run's notes:\n" + "".join(f"- {x}\n" for x in notes)
    if isinstance(at, (int, float)):
        from . import activity
        try:
            ran = activity.before_boot(float(at), tz)
        except Exception:   # an unreadable log must not stop the prompt
            ran = []
        if ran:
            out += (f"Locks and detached jobs on this host in the {activity.REBOOT_WINDOW_S // 60} min before the "
                    "boot, from every project here (one of them may explain it):\n" + "".join(f"- {x}\n" for x in ran))
    return out


# Stands in a worker prompt for its run's own directory, which exists only once the run is recorded:
# start_run puts the path in.
RUN_DIR_MARK = "<this run's dir>"


def worker_task(p: Project, task: dict, cwd: str, branch: str | None, wake: dict | None = None,
                conflict: dict | None = None) -> str:
    """The per-task part of a worker's prompt, after worker_system(): the rules for its kind, the
    task itself, and a pointer back at the restrictions at the end. `wake` (the daemon's run note) marks a run
    that wakes a waiting task, at a tier that may be below the task's own. `conflict` (the run
    note's conflict_fix) marks a push-conflict fix: a light one gets conflict_fix_task's short prompt."""
    cfg = p.config()
    kind = task["kind"] or "work"
    history = ""
    prev = load_result(task["result"])
    cmark = prev.get(conflictfix.KEY) if isinstance(prev.get(conflictfix.KEY), dict) else None
    if conflict and cmark and conflict.get("tier") == "light":
        return conflict_fix_task(p, task, cwd, branch, cmark)
    if prev:
        history = f"\nPrevious attempt ended '{prev.get('status')}': {str(prev.get('summary') or '')[:1500]}\n"
        if prev.get("woke"):
            history += f"Woken because: {prev['woke']}.\n"
        if conflict and cmark and cmark.get("trim"):
            history += (f"This run trims the change to fit its prompt size caps at {conflict.get('tier')} "
                        f"({conflict.get('why')}); it costs no review attempt. Rebase onto the tip, trim the "
                        f"change's own prompt text by at least the overshoot ({conflictfix.over_text(cmark['trim'])}) "
                        "without dropping a rule or raising a cap, run the failing cap tests and approve the new head.\n")
        elif conflict and cmark:
            history += (f"This run settles that push conflict at {conflict.get('tier')} ({conflict.get('why')}); it "
                        "costs no review attempt. Rebase, resolve keeping both sides' intents, run focused tests "
                        "of what the resolution touched and approve the new head.\n")
        history += _reboot_note(prev.get("reboot"), timefmt.home(p))
    run_tier = (wake or {}).get("tier") or task["tier"]
    step = bud.mechanical_step(prev) if wake else ""
    if wake and not step and bud.next_step(prev):
        history += f"The last run left this next step for after its wait: {bud.next_step(prev)}\n"
    if step and not wake.get("escalated"):
        history += (f"This run is a {run_tier} wake for one mechanical step the last run left: {step}. If the wait "
                    "is over, do that step in this run and hand off its outcome. Only if it stops being mechanical "
                    "(a conflict to resolve, a failure to judge)"
                    + (f", hand off `waiting` with `retry_after_s: 0` and `wake_tier: \"{task['tier']}\"`: the task "
                       "runs again now at its own tier, without costing an attempt; leave out `next_step` then.\n"
                       if run_tier != task["tier"]
                       else ", go on with it in this run.\n"))
    elif wake and run_tier != task["tier"] and not wake.get("escalated"):
        history += (f"This run is a {run_tier} wake: check whether the wait is over. If it is and substantial "
                    f"work remains, hand off `waiting` with `retry_after_s: 0` and `wake_tier: \"{task['tier']}\"` "
                    f"at once: the task runs again now at its own tier, without costing an attempt.\n")
    if wake and not wake.get("escalated"):
        stale = bud.stale_wakes(prev)
        cap = int((cfg.get("waiting") or {}).get("max_stale_wakes", 3) or 0)
        if stale:
            history += (f"This wait came back unchanged from the last {stale} wake(s)"
                        + (f"; at {cap} the task goes to the coordinator to split or re-plan" if cap else "")
                        + ". If it is still not over, hand off `waiting` with a `retry_when` probe that exits 0 "
                          "once it is: the daemon checks it without a model.\n")
        if not str(prev.get("retry_when") or "").strip():
            history += ("The last hand-off gave no `retry_when`, so only a model run could check this wait. "
                        "If you hand off `waiting` again, give one.\n")
    # A review with a push step run while a setting hold stands: review only, then blocked.
    history += settinghold.prompt_line(p.db, cfg, task, p.root)
    if branch:
        history += recovery_note(p, task)
    # A follow-up of done work (a daemon-queued continue hop) starts fresh but reads its parent's summary.
    old_id = continues_id(task) or chaincap.follows_id(task)
    old = p.db.task(old_id) if old_id else None
    if old:
        on = f" on branch {old['branch']}" if own_worktree(old) and old["branch"] and continues_id(task) else ""
        was = str(load_result(old["result"]).get("summary") or old["blocked_reason"] or "")[:1500]
        history += (f"\nThis task continues #{old['id']} {old['title']} ({old['status']}){on}"
                    + (", and starts from that branch's head" if on and (branch or gets_worktree(p, kind)) else "")
                    + (", and starts from the head of what it reviewed" if old["kind"] == "review" and branch else "")
                    + (f". Its last summary: {was}" if was else "") + "\n")
    if kind == "code" and not branch:
        history += (f"\nNo worktree was made for this task: make your own, t{task['id']} on branch "
                    f"ttp/t{task['id']}-<topic>, in the repo you change"
                    + (f" (from #{old['id']}'s branch; never edit in its worktree)" if old and continues_id(task) else "")
                    + f": `git worktree add <dir>/t{task['id']} -b ttp/t{task['id']}-<topic> <start>`, <start> "
                    "the branch you build on. Never edit another task's worktree.\n")
    history += verified_line(p, task, cwd)
    own_off = own_push(cfg) == "off"
    if own_off and branch and (onto := carried_branch(task) or rebased_branch(task)):
        history += (f"\nThis task builds on branch {onto}, but own pushes are off in this project "
                    f"(delivery.own_push): it lands on the push branch like any other change. Never push to "
                    f"{onto} or any other branch yourself (no own-branch push, no `git push`).\n")
    elif (onto := carried_branch(task)) and branch:
        history += (f"\nThis task delivers onto branch {onto} (`ttp push --own --detach`, fast-forward only)"
                    + (": its worktree is on it.\n" if branch == onto else
                       f": it could not be checked out here, so this task works on its own branch, from that "
                       f"branch's head when there is one, and the push publishes its head onto {onto}.\n"))
    elif (old_branch := rebased_branch(task)) and branch and rebase_replace(cfg) != "lease":
        history += (f"\nThis task rebases branch {old_branch}: it starts from that branch's head on its own branch "
                    f"{branch} and delivers there (`ttp push --own --detach`), never onto {old_branch}: a rebase "
                    f"cannot fast-forward it.\n")
    elif (old_branch := rebased_branch(task)) and branch:
        history += (f"\nThis task rebases branch {old_branch}: it starts from that branch's head on its own branch "
                    f"{branch}, and `ttp push --own --detach` publishes its head back onto {old_branch}. A rebase "
                    f"cannot fast-forward it, so that push replaces it only when HEAD holds every commit "
                    f"{old_branch} gained since your start (else it refuses, naming them: rebase again onto its "
                    f"new tip), then under a lease on the tip it reads (never a plain force push, never "
                    f"`git push`), and never creates it.\n")
    delivery = cfg.get("delivery", {})
    venv = project_venv(p, cwd)
    venv_line = (f"python venv: {venv} (the project's, already active: VIRTUAL_ENV and PATH; shared with other "
                 "workers, so do not install into it; its editable installs import the project root's code, not "
                 "yours; need other packages? make a venv of your own in the working directory)\n") if venv else ""
    parts = [
        _sections(_read(p, f"kind-{kind}.md"), cfg),
        f"# YOUR TASK #{task['id']}: {task['title']}\n"
        f"kind: {kind} · tier: {task['tier']}" + (f" · this run: {run_tier} wake" if wake else "")
        + f" · attempt {int(task['attempts'] or 0) + 1} of "
        f"{task['max_attempts']} · budget ${task['budget_usd'] or 0:.2f} (spent ${task['spent_usd'] or 0:.2f})\n"
        f"working directory: {cwd}" + (f" · branch: {branch}" if branch else "") + "\n"
        + (f"This is a git worktree of your own (worktree.kinds): commit changes to tracked files on {branch}; "
           "it is not merged anywhere by itself.\n" if branch and kind != "code" else "")
        + f"run dir: {RUN_DIR_MARK} ($TTP_RUN_DIR): the hand-off goes only to its result.json, never to a run "
        "dir named in earlier context\n"
        + _resource_line(task)
        + _runner_line(cfg)
        + _hostmap_line(cfg)
        + f"project root: {p.root}\n"
        + root_behind_line(p, cwd)
        + _zone_line(p)
        + venv_line
        + f"delivery policy: draft PRs={delivery.get('draft_prs', True)}, review before PR="
        f"{delivery.get('review_before_pr', True)}, auto-merge repos={delivery.get('auto_merge_repos') or 'none'}, "
        f"push allowed={delivery.get('push_allowed', True)}"
        + (f", code tasks may land on {delivery['push_branch']} with ttp push=True"
           if kind == "code" and code_tasks_may_push(cfg) else "")
        + (", push queue=True" if kind == "review" and push_queue_on(cfg) else "")
        + (f", own pushes={own_push(cfg)} (delivery.own_push: "
           + ("refused; never push any branch but the push branch)" if own_off else
              "`ttp push --own` publishes only onto a branch the remote already has)")
           if own_push(cfg) != "on" and delivery.get("own_push") is not None else "") + "\n"
        f"{history}\n## Spec\n{task['spec'] or task['title']}\n",
        replymsg.prompt_block(dict(task)),
        settled.prompt_block(p, dict(task)),
        taskfacts.prompt_block(p, dict(task)),
        overlap.prompt_block(p, dict(task)),
        restrictions_pointer(p)]
    return "\n\n".join(x for x in parts if x.strip())


def conflict_fix_task(p: Project, task: dict, cwd: str, branch: str | None, cf: dict) -> str:
    """The short prompt of a light push-conflict fix (conflictfix.py): rebase the approved head onto
    the push branch's tip, test what the resolution touched, and approve the new head again. The
    review's own rules and spec stay out: the change was already reviewed."""
    head, onto, target = str(cf.get("head") or "?"), str(cf.get("onto") or "the tip"), str(cf.get("target") or "?")
    cb = str(cf.get("branch") or "")
    if isinstance(cf.get("trim"), dict):
        return _trim_fix_task(p, task, cwd, branch, cf, head, onto, target, cb)
    files = ", ".join(str(f) for f in cf.get("files") or []) or "?"
    return "\n\n".join([
        f"# YOUR TASK #{task['id']}: push-conflict fix for {task['title']}\n"
        f"kind: review · this run: light conflict fix (no review attempt) · budget ${task['budget_usd'] or 0:.2f} "
        f"(spent ${task['spent_usd'] or 0:.2f})\n"
        f"working directory: {cwd}" + (f" · branch: {branch}" if branch else "") + "\n"
        f"run dir: {RUN_DIR_MARK} ($TTP_RUN_DIR): the hand-off goes only to its result.json\n"
        f"project root: {p.root}\n" + _zone_line(p),
        f"This review already approved branch {cb or '(none)'} at {head}, but the push queue could not land it: "
        f"it conflicts with {target} at {onto} in {files}.\n\n"
        "Do only this:\n"
        f"1. Fetch, then rebase {head} onto the current tip of {target} here, resolving each conflict keeping both "
        "sides' intents. That resolution is the one edit you may make.\n"
        "2. Run only the focused tests of the files the resolution touched (a file, `-k` or `file::test`; never "
        "the full suite), plus the validator of the package they belong to where the repository documents one "
        "(e.g. a plugin validator). The push queue runs the full push checks on the head you hand back.\n"
        f"3. Show `git range-diff $(git merge-base {head} <new tip>)..{head} <new tip>..HEAD` and say in the "
        "summary whether each commit matches apart from the resolution.\n"
        "4. Commit and hand off `done` with "
        f'"push": [{{"branch": "{cb}", "head": "<the rebased head, full hash>"}}].\n\n'
        "If both sides changed the same function or test, the resolution needs a decision about either "
        "change's intent, or the tests fail for a reason you cannot settle in the resolution: stop and hand off "
        "`failed` saying what it is. The task then runs again at once at standard, at no attempt.\n"
        "Never run `ttp push` or `git push`, never bump versions or write a changeset.",
        restrictions_pointer(p)])


def _trim_fix_task(p: Project, task: dict, cwd: str, branch: str | None, cf: dict, head: str, onto: str,
                   target: str, cb: str) -> str:
    """conflict_fix_task for a trim-to-fit fix (conflictfix.cap_failure): the merged push checks failed
    only on prompt size caps, so the run trims this change's own prompt text by the overshoot."""
    trim = cf["trim"]
    tests = " ".join(str(i) for i in trim.get("ids") or []) or "the failing cap tests"
    return "\n\n".join([
        f"# YOUR TASK #{task['id']}: trim-to-fit fix for {task['title']}\n"
        f"kind: review · this run: light trim-to-fit fix (no review attempt) · budget ${task['budget_usd'] or 0:.2f} "
        f"(spent ${task['spent_usd'] or 0:.2f})\n"
        f"working directory: {cwd}" + (f" · branch: {branch}" if branch else "") + "\n"
        f"run dir: {RUN_DIR_MARK} ($TTP_RUN_DIR): the hand-off goes only to its result.json\n"
        f"project root: {p.root}\n" + _zone_line(p),
        f"This review already approved branch {cb or '(none)'} at {head}, but the push queue could not land it: "
        f"merged onto {target} at {onto}, the push checks failed only on prompt size caps. The change fit alone; "
        f"with what landed before it, the prompt text is over: {conflictfix.over_text(trim)}.\n\n"
        "Do only this:\n"
        f"1. Fetch, then rebase {head} onto the current tip of {target} here, resolving any conflict keeping both "
        "sides' intents.\n"
        f"2. Run the failing cap tests ({tests}) and note each size against its cap at the new tip.\n"
        "3. Trim this change's own added or edited prompt text until each passes, by at least the overshoot: "
        "tighter wording, no repetition. Never drop or weaken a rule or restriction, never trim text the change "
        "did not add, never raise a cap. That trim is the one edit you may make.\n"
        "4. Run those tests again plus the validator of the package they belong to where the repository documents "
        "one. The push queue runs the full push checks on the head you hand back.\n"
        "5. Say in the summary what you trimmed and each size against its cap now. Commit and hand off `done` with "
        f'"push": [{{"branch": "{cb}", "head": "<the new head, full hash>"}}].\n\n'
        "If the change cannot fit without dropping a rule, stop and hand off `failed` saying so. The task then "
        "runs again at once at standard, at no attempt.\n"
        "Never run `ttp push` or `git push`, never bump versions or write a changeset.",
        restrictions_pointer(p)])


# A fetch of the push branch in the project root, for root_behind_line: bounded, and at most once per
# ROOT_FETCH_TTL_S per root and branch (the daemon builds prompts in its dispatch loop).
ROOT_FETCH_TIMEOUT_S = 20
ROOT_FETCH_TTL_S = 300
_root_fetched: dict[str, float] = {}


def root_behind_line(p: Project, cwd: str) -> str:
    """For a task that runs in the project root: a line saying how many commits its HEAD lacks of the
    push branch's remote ref, fetched, and how to read current code without moving HEAD. "" when `cwd`
    is not the root, the root is current, has no push branch or remote, or the fetch fails."""
    from . import push
    try:
        root = Path(p.root)
        if not cwd or Path(cwd).resolve() != root.resolve():
            return ""
        if not str((p.config().get("delivery") or {}).get("push_branch") or "").strip() or push.remote_less(root):
            return ""
        remote, branch = push.target(p, root)
        ref, key = f"{remote}/{branch}", f"{root}\0{remote}/{branch}"
        if time.time() - _root_fetched.get(key, 0.0) >= ROOT_FETCH_TTL_S:
            got = push._git_net(root, "fetch", "-q", "--no-tags", "--no-write-fetch-head", "--no-auto-maintenance",
                                remote, f"+refs/heads/{branch}:refs/remotes/{ref}", timeout_s=ROOT_FETCH_TIMEOUT_S)
            if got.returncode != 0:
                return ""
            _root_fetched[key] = time.time()
        n = push._git(root, "rev-list", "--count", f"HEAD..refs/remotes/{ref}").stdout.strip()
    except Exception:   # an unreadable root must not stop the prompt
        return ""
    if not n.isdigit() or int(n) == 0:
        return ""
    return (f"project root checkout: HEAD is {n} commit{'s' if int(n) != 1 else ''} behind {ref}, so its files "
            f"are old. Read current code with `git show {ref}:<path>` or `git grep <pattern> {ref}`; never move "
            "the root's HEAD (no checkout, pull, reset or rebase there).\n")


def verified_line(p: Project, task: dict, cwd: str) -> str:
    """What a task building on verified work verifies (verified.py): only the commits after its lineage's
    marker while that is an ancestor of HEAD, else everything, said either way. "" with no marker or no
    git HEAD in `cwd`."""
    import subprocess
    from . import verified
    try:
        if not continues_id(task):
            return ""
        head = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=30).stdout.strip() if cwd else ""
        m = verified.scope(p.db, task, cwd, head) if head else None
    except Exception:
        return ""
    if not m:
        return ""
    if not m["ok"]:
        return (f"\nInherited marker: {m['line']}. Verify the whole change, inherited commits included.\n")
    return (f"\nInherited marker: {m['line']}. Verify only the commits after it (`git log {m['sha'][:12]}..HEAD`) "
            f"and your own: do not re-review or re-test the inherited commits. `ttp checks` scopes its "
            f"if_changed checks the same way; the push checks still run in full on the final head.\n")


def spec_digest(task: dict) -> str:
    """A short fingerprint of the task's spec, kept with each run, so a resumed session learns of a
    spec that changed while it was cut off."""
    return hashlib.sha256(str(task["spec"] or task["title"]).encode()).hexdigest()[:16]


def worker_resume(p: Project, task: dict, lost: dict) -> str:
    """The prompt that continues the agent session of a run the host took away (`lost`, from the
    daemon): what happened, what is gone, what changed since, then the pointer back at the restrictions
    (worker_system(), which holds them, comes with it again). The session already holds the task and its
    own work."""
    at = lost.get("ended")
    when = timefmt.long(at, timefmt.home(p)) if isinstance(at, (int, float)) else "recently"
    if lost.get("cause") == "dry_run":   # its hand-off stands but its on_pass_cmd cannot run (onpassdry)
        text = (f"# Continue task #{task['id']}: {task['title']}\n"
                f"Your waiting hand-off was not taken: {lost.get('dry_run')}\n"
                f"This continues your session and does not count as an attempt (budget "
                f"${task['budget_usd'] or 0:.2f}, spent ${task['spent_usd'] or 0:.2f}). Your detached jobs "
                f"still run: do not start them again.\n")
        if lost.get("dir"):
            text += (f"This run has a new run directory, {RUN_DIR_MARK}: write the hand-off only to "
                     f"$TTP_RUN_DIR/result.json, not under {lost['dir']}; a retry_when or on_pass_cmd that "
                     f"names files under {lost['dir']} still reads them there.\n")
        return "\n\n".join(x for x in (text, restrictions_pointer(p)) if x.strip())
    why = {"reboot": "the host rebooted", "sleep": "the host slept",
           "network": "the network went away"}.get(lost.get("cause"), "its supervisor was lost")
    free = lost.get("cause") in ("reboot", "sleep", "network")
    text = (f"# Continue task #{task['id']}: {task['title']}\n"
            f"Your run was cut off at about {when}: {why}. This continues your session"
            + (" and does not count as an attempt" if free else "")
            + f" (attempt {int(task['attempts'] or 0) + 1} of {task['max_attempts']}, "
            f"budget ${task['budget_usd'] or 0:.2f}, spent ${task['spent_usd'] or 0:.2f}).\n"
            "Detached jobs, /tmp files and device state from before then are gone. Check `git status` and "
            "the job logs, then carry on where you left off; do not redo finished work.\n")
    old_dir = lost.get("dir")
    if old_dir:
        text += (f"This run has a new run directory, {RUN_DIR_MARK}: write the hand-off only to "
                 f"$TTP_RUN_DIR/result.json, not under {old_dir} or any run dir named in earlier context.\n")
    update = []
    if lost.get("spec_sha") and lost["spec_sha"] != spec_digest(task):
        update.append(f"The spec changed:\n{task['spec'] or task['title']}")
    steer = unread_update(Path(old_dir))[0] if old_dir else ""
    if steer:
        update.append(steer)
    if update:
        text += ("\nUpdate for your task from the project coordinator. Where it differs from the spec, it wins:\n"
                 + "\n".join(update) + "\n")
    return "\n\n".join(x for x in (text, restrictions_pointer(p)) if x.strip())


def worker_prompt(p: Project, task: dict, cwd: str, branch: str | None) -> str:
    """The whole prompt in one piece, as a worker reads it."""
    return worker_system(p, task) + "\n\n" + worker_task(p, task, cwd, branch)
