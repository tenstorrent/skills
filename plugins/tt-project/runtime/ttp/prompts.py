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
from . import chaincap, conflictfix, ctxblock, overlap, relay, replymsg, settinghold, settled, taskfacts, tierfloor, timefmt
from . import knownpaths, preempt, sidefx, specfold, streamguard
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


def _raw(p: Project, name: str) -> str:
    """Harness prompt `name` as written, fences and all, for _sections to pick its blocks."""
    f = p.harness / "prompts" / name
    return f.read_text() if f.exists() else ""


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
    if relay.agent_orders(cfg):
        on.add("agent_orders")
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
    on.add("session")   # the persistent coordinator, always on: its self-improvement loop (selfimprove)
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
    features only. The persistent session's own rules (`session`) go only in its role, coordinator_session_role:
    this stateless role keeps its landed size budget."""
    return feature_blocks((p.harness / "prompts" / "coordinator.md").read_text(), coordinator_features(p) - {"session"})


def coordinator_session_role(p: Project, tools: tuple[str, str] = ("full", "")) -> str:
    """The persistent session's role (coordinator.md, then prompts/coordinator-session.md: how wakes
    continue one session and compact it in place), under the coordinator role's size budget. It
    always carries the order ledger's rules: the first open order or the last closed one would
    otherwise change the prompt and start a new session. Its Tools section names
    only the wake's real tools (session_tools): `tools` is (full, read or none, why)."""
    from .coordtools import tools_mode
    state, why = tools
    if state == "full" and tools_mode(p.config()) != "full":
        state, why = "read", "`coordinator.session.tools` is read_only"
    on = coordinator_features(p) | {"obligations", f"tools_{state}"}
    role = feature_blocks((p.harness / "prompts" / "coordinator.md").read_text(), on)
    return f"{role}\n\n{_read(p, 'coordinator-session.md', on)}".rstrip().replace("{tools_why}", why) + "\n"


def session_tools(p: Project, provider: str | None = None) -> tuple[str, str]:
    """What a full-tools session wake of coordinator `provider` (unset: claude) really gets
    (Provider.coord_tool_set), in the folder its read-only turn runs in (daemon.start_run)."""
    from .providers import get_provider
    from .providers.base import scratch_dir
    prov = get_provider(provider or "claude")
    return prov.coord_tool_set(scratch_dir(str(p.base)) if prov.isolate_read_only else str(p.base))


# kind-review.md's two delivery sections; a review's prompt carries only the one its project uses.
REVIEW_PUSH_SECTIONS = ("## Pushing a reviewed change", "## Approving into the push queue")


OWN_SECTION = "## Publishing onto its own PR branch"   # kind-review.md's `ttp push --own` section


SPLIT_MARK = "{{split_lines}}"   # kind-code.md's split threshold, filled in from chains.split_lines


def split_lines(cfg: dict) -> int:
    """chains.split_lines: the non-test lines past which a code worker splits its change; 0 when off or unreadable."""
    try:
        return max(0, int((cfg.get("chains") or {}).get("split_lines") or 0))
    except (TypeError, ValueError):
        return 0


def _sections(text: str, cfg: dict) -> str:
    """A kind prompt without the push section that does not apply: the plain push's or the queue's,
    the `ttp push --own` advice while own pushes are off (delivery.own_push), its upstream note
    rules while this project does not read the upstream inbox (upstream.ingest), and its split rule
    while chains.split_lines is off (else with that threshold filled in)."""
    n = split_lines(cfg)
    on = ({"upstream_ingest"} if (cfg.get("upstream") or {}).get("ingest") else set()) | ({"split_lines"} if n else set())
    text = feature_blocks(text, on).replace(SPLIT_MARK, str(n))
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


# A heading that is the coordinator's own and so ends a Brief that has headings of its own: the template's
# sections after the Brief and the dated ones charter_update adds
BRIEF_END_HEADS = ("goal", "polic", "resource", "responsib")
BRIEF_END_RE = re.compile(r"^\s*(?:<!--\s*)?\(?end of (?:the )?brief\)?\.?\s*(?:-->)?\s*$", re.I)


def brief_lines(charter: str) -> list[bool]:
    """Per line of `charter` (splitlines), whether it is the Brief's: from a `## Brief ...` heading up to
    the next coordinator section (BRIEF_END_HEADS, a heading charter_update dated "(added ...)", or the
    template's "Restrictions (binding on every task)") or an explicit end line (`<!-- end of Brief -->`,
    "(end of Brief)"). A Brief that has `## ` headings of its own (Why, Deliverables, Restrictions ...)
    keeps them: they are the user's words too. A `## Restrictions` heading ends a Brief that has none of
    its own before it (a one-paragraph Brief, then the coordinator's Restrictions)."""
    out, on, own = [], False, False
    for line in charter.splitlines():
        if line.startswith("## "):
            name = " ".join(line[3:].split()).lower()
            if name.startswith("brief"):
                on, own = True, False
            elif on and (name.startswith(BRIEF_END_HEADS) or "(added " in name or "binding on every task" in name
                         or name.startswith("restriction") and not own):
                on = False
            elif on:
                own = True
        elif on and BRIEF_END_RE.match(line):
            on = False
        out.append(on)
    return out


def brief_text(charter: str) -> str:
    """The Brief's lines (brief_lines), joined."""
    return "\n".join(ln for ln, on in zip(charter.splitlines(), brief_lines(charter)) if on)


def brief_restrictions(charter: str) -> set[str]:
    """The items (whitespace-joined) of the Restrictions sections inside the Brief: they rest on the
    Brief (origin Brief), not on a cite of their own."""
    from .restriction_source import split_items
    body, keep = [], False
    for line, on in zip(charter.splitlines(), brief_lines(charter)):
        if line.startswith("## "):
            keep = on and line[3:].strip().lower().startswith("restriction")
        elif keep and on:
            body.append(line.rstrip())
    return {" ".join(" ".join(it).split()) for it in split_items("\n".join(body))}


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


def restrictions_block(p: Project, advisory: bool = True) -> str:
    """The charter's sourced Restrictions items as the BINDING block, then (`advisory`) its unsourced
    ones apart, marked not binding (restriction_source)."""
    from .restriction_source import partition
    charter = p.charter_path.read_text() if p.charter_path.exists() else ""
    binding, loose = partition(charter_restrictions(charter), brief_restrictions(charter))
    out = [("# BINDING RESTRICTIONS (from the charter — override the task, the harness "
            "rules and your own judgement; if a step would break one, do not take it)\n" + binding)] if binding else []
    if loose and advisory:
        out.append("# Unsourced restrictions (advisory, not binding: they cite no user message, the Brief or a "
                   "harness gate; the coordinator drops them)\n" + loose)
    return "\n\n".join(out)


def restrictions_pointer(p: Project) -> str:
    """The line that closes a worker's task prompt in place of a second copy of restrictions_block():
    the block itself leads worker_system(), which every worker run gets once, as its system prompt or
    leading its prompt."""
    return ("The BINDING RESTRICTIONS block in your instructions binds this task: it overrides the task, "
            "the harness rules and your own judgement; if a step would break one, do not take it.") if restrictions_block(p, advisory=False) else ""


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


def _blocks(body: list[str]) -> list[str]:
    """A section's body as its blank-line-separated blocks (a paragraph with the list under it is one)."""
    return [x.strip("\n") for x in re.split(r"\n\s*\n", "\n".join(body)) if x.strip()]


def goal_for_workers(item: str, resource: bool = False) -> bool:
    """Whether an item of a section workers get whole (Goals, Resources, the Brief) is in a worker's
    prompt: its audience tag wins; untagged, all but an item that names the coordinator or the daily
    review (the coordinator's) and never a worker: its own duties, which the full charter keeps.
    `resource`: an item of Resources, kept whole unless tagged: a list there is one block, and one
    bullet naming the coordinator must not drop the machines and repos the others name."""
    tag = _AUDIENCE_TAG.search(item)
    if tag:
        return tag[1].lower().startswith("worker")
    return resource or (not re.search(r"\bcoordinator|\bdaily review", item, re.I)
            or bool(re.search(r"\bworkers?\b", item, re.I)))


_CHARTER_HEAD = ("# CHARTER (for workers: goals, resources and the policies for workers; its restrictions are "
                 "the binding block above. Full charter{more}: {path})\n")
_MEMORY_HEAD = "# PROJECT MEMORY (entries for workers; all {n} entries: {path})\n"


def _worker_heads(p: Project, dropped: int, n: int) -> tuple[str, str]:
    more = f"; {dropped} more items for the coordinator" if dropped else ""
    return _CHARTER_HEAD.format(more=more, path=p.charter_path), _MEMORY_HEAD.format(n=n, path=p.memory_index)


# Heads the pointers to the memory pinned for workers that its prompt has no room for whole (see
# Project.worker_memory_select): where each is in full; not a fault.
_PINNED_MORE = "Shortened for room, each in full in {path}/<name>.md (read those your task touches):"


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
                   entries: list[dict] | None = None, reserve: int = 0) -> tuple[str, list[dict], dict]:
    """A worker's digest of the charter and memory: the Goals and Resources whole (the Brief only when
    no Goals are stated) and the policies and other charter items meant for workers (see
    for_workers) within what those leave of WORKER_MEMORY_CHARS, never less than
    WORKER_POLICY_MIN_CHARS; then memory pinned for workers (see worker_memory) in
    what is left of it, never less than WORKER_PINNED_MIN_CHARS, and the entries that share words with
    `task`, those that speak of workers and the newest facts while all still fit WORKER_MEMORY_CHARS,
    or WORKER_RANKED_MIN_CHARS past the pinned ones if that is more (see worker_memory_select).
    Memory never pushes out a policy. Memory entries that restate the charter's restrictions are left
    out: the prompt states those whole in their own block. Pinned entries past their budget are
    shown as pointers (`pointers`, counted in `pinned_dropped`; worker_system says where to read them). `entries`: memory entries in
    place of memory/'s (see Project.worker_memory_select). `reserve`: chars of the entries that are not
    pinned left for the task's own prompt (worker_rules). Returns (charter text, memory entries, usage)."""
    raw = p.charter_path.read_text() if p.charter_path.exists() else ""
    secs = [(h, b) for h, b in charter_sections(charter_without_placeholders(raw))
            if h and not h[3:].strip().lower().startswith("restriction")]
    name = lambda h: h[3:].strip().lower()
    goals = any(name(h).startswith("goal") for h, _ in secs)
    whole = lambda h: name(h).startswith(("goal", "resource")) or (not goals and name(h).startswith("brief"))
    heads = sum(len(x) + 2 for x in _worker_heads(p, 999, 99999))   # their longest
    keep = lambda h, x: goal_for_workers(x, name(h).startswith("resource"))
    used = heads + sum(len(h) + len("\n\n".join(x for x in _blocks(b) if keep(h, x))) + 3
                       for h, b in secs if whole(h))
    out, dropped = [], 0
    cap = max(WORKER_MEMORY_CHARS, used + WORKER_POLICY_MIN_CHARS)   # long Goals never squeeze the policies out
    for h, b in secs:
        if whole(h):
            blocks = _blocks(b)
            kept = [x for x in blocks if keep(h, x)]
            dropped += len(blocks) - len(kept)
            if kept:
                out.append(h + "\n" + "\n\n".join(kept))
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
    mem, use = p.worker_memory_select(max(0, rest - reserve), task, room, restated, entries)
    if use["pinned_dropped"]:   # make room for the line heading the pointers
        room -= len(_PINNED_MORE.format(path=p.memory_dir)) + 1
        mem, use = p.worker_memory_select(max(0, min(rest, room) - reserve), task, room, restated, entries)
    elif use["pinned_chars"] + WORKER_RANKED_MIN_CHARS > rest:   # every pinned one shown: the others' floor
        mem, use = p.worker_memory_select(use["pinned_chars"] + WORKER_RANKED_MIN_CHARS - reserve, task, room,
                                          restated, entries)
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
    # Machines this project may use (its Resources or Restrictions name them) and the ones this task
    # names: a machine a goal or a policy only mentions turns on no rules for every task.
    scope = resources + " " + charter_restrictions(charter)
    used = {a: m for a, m in known.items() if _named(a, scope) or _named(a, task_text)}
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
    if re.search(r"\b(slurm|cluster)", scope + " " + task_text, re.I) or any(
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


# Features a task's kind or spec turns on, never the project alone. Their blocks of worker.md go in the
# task prompt (worker_rules), never in worker_system, which is then the same for every worker and
# reviewer of a project: concurrent runs share its cache.
TASK_FEATURES = ("harness", "prs", "wait", "cells")


def project_features(p: Project) -> set[str]:
    """The features of worker_features that no task changes: those worker_system renders."""
    return worker_features(p) - set(TASK_FEATURES)


def split_rules(text: str, base: set[str], on: set[str]) -> tuple[str, list[tuple[str, str]]]:
    """`text` (worker.md) as feature_blocks renders it for `base` (project_features), but without the
    blocks of TASK_FEATURES nor those of a feature `base` leaves off: the same for every task. Then each
    left-out block that `on` (worker_features of a task) turns on, rendered for `on`, as (where, text):
    `where` names its section, the item it continues when it starts mid-item outside a code fence, and the fence. A `!X` block of a
    feature outside TASK_FEATURES follows `base` alone."""
    shared, extras, stack, cap = [], [], [], None
    section = item = fence = ""
    for line in text.splitlines(keepends=True):
        m = _IF_RE.match(line)
        if cap is not None:   # inside a left-out block: kept whole, nested blocks too
            cap[1].append(line)
            cap[0] += (-1 if m.group(1) else 1) if m else 0
            if not cap[0]:
                _, raw, x, neg, (sec, cont, fen) = cap
                body = feature_blocks("".join(raw), on) if (x in on) != neg else ""
                if body.strip():   # a block that starts mid-item names the item it continues
                    extras.append(((sec, cont if body.lstrip("\n")[:1].isspace() else "", fen), body))
                cap = None
            continue
        if m and not m.group(1):
            neg, x = bool(m.group(2)), m.group(3)
            if not any(stack) and (x in TASK_FEATURES or (x not in base and not neg)):
                cont = f'(continues "{item.strip()[:16]}…")' if item and not fence else ""
                cap = [1, [line], x, neg, (section, cont, fence)]
            else:
                stack.append(any(stack) or (x in base) == neg)
            continue
        if m:
            if stack:
                stack.pop()
            continue
        if any(stack):
            continue
        shared.append(line)
        if line.startswith("```"):
            fence = "" if fence else line.strip()
        elif line.startswith("#") and not fence:
            section, item = line.strip().lstrip("#").strip(), ""
        elif line.strip() and not line[0].isspace():
            item = line
    return strip_markers("".join(shared)), extras


def worker_rules(p: Project, task: dict) -> str:
    """What worker_system leaves out for `task` and its own prompt carries: the worker.md blocks of
    the features it alone turns on (split_rules), and the memory entries its words pick
    (worker_memory_select) that worker_system's task-free choice does not show whole, while they fit
    WORKER_RANKED_MIN_CHARS."""
    f = p.harness / "prompts" / "worker.md"
    rules = split_rules(f.read_text(), project_features(p), worker_features(p, task))[1] if f.exists() else []
    out, last = [], None
    for (section, cont, fence), body in rules:   # one heading per section, one line per item continued
        body = body.strip("\n")
        if out and last[:2] == (section, cont) and last[2] == fence:
            out[-1] = out[-1][:-4] + "\n" + body + "\n```" if fence else out[-1] + "\n" + body
        else:
            out.append(("" if last and last[0] == section else f"## {section or 'Worker rules'}\n")
                       + (cont + "\n" if cont else "") + (f"{fence}\n{body}\n```" if fence else body))
        last = (section, cont, fence)
    shown = {e["name"] for e in worker_context(p, reserve=WORKER_RANKED_MIN_CHARS)[1]}
    mem, used = [], 0
    for e in worker_context(p, task)[1]:
        if e["name"] not in shown and used + len(e["line"]) + 1 <= WORKER_RANKED_MIN_CHARS:
            mem.append(e)
            used += len(e["line"]) + 1
    parts = []
    if out:
        parts.append(f"# {TASK_RULES_HEADER}\n" + "\n\n".join(out))
    if mem:
        p.note_memory_shown([e["name"] for e in mem])
        parts.append(f"# {TASK_MEMORY_HEADER}\n" + "\n".join(e["line"] for e in mem))
    return "\n\n".join(parts)


TASK_RULES_HEADER = "TASK RULES (binding, as in the system prompt's section named)"
TASK_MEMORY_HEADER = "MORE MEMORY FOR THIS TASK"


def worker_corrections(p: Project) -> list[dict]:
    """The user's corrections a worker's prompt lists ahead of its memory (Project.user_corrections)."""
    return [e for e in p.user_corrections() if worker_memory(e)]


def worker_system(p: Project, task: dict | None = None) -> str:
    """The part of a worker's prompt shared by every worker and reviewer of the project until the
    charter, its rules or its machines change: sent as the system prompt where the agent allows it, so
    the provider caches it across runs (back to back, same model). It never reads `task` (kept for
    callers), nor memory, which changes far more often: worker_memory_block, in the run's own prompt."""
    f = p.harness / "prompts" / "worker.md"
    parts = [restrictions_block(p), split_rules(f.read_text(), project_features(p), set())[0] if f.exists() else "",
             knownpaths.block(p)]
    # The one copy of the restrictions (the task prompt only points back at it); a second inside the
    # charter only costs tokens.
    # The rest of the charter is mostly the coordinator's: workers get their digest of it.
    charter, _, use = worker_context(p, reserve=WORKER_RANKED_MIN_CHARS)
    parts.append(_worker_heads(p, use["charter_dropped"], 0)[0] + (charter or "(none)"))
    return "\n\n".join(x for x in parts if x.strip())


def worker_memory_block(p: Project) -> str:
    """The user's corrections and the project memory for workers (worker_context's task-free choice),
    leading a run's own prompt: one new entry must not change the system prompt every worker and
    reviewer shares, which would make each one's first call write the cache afresh (task #51)."""
    _, mem, use = worker_context(p, reserve=WORKER_RANKED_MIN_CHARS)   # the task's own: worker_rules
    # The user's corrections come before the rest of memory, outside its budget (Project.user_corrections).
    fixed = worker_corrections(p)
    p.note_memory_shown([e["name"] for e in fixed + mem + use["pointers"]])
    parts = []
    if fixed:
        parts.append(f"# {CORRECTIONS_HEADER.upper()}\n" + "\n".join(e["line"] for e in fixed))
    more = [_PINNED_MORE.format(path=p.memory_dir)] + [x["line"] for x in use["pointers"]] if use["pointers"] else []
    if mem or more:
        parts.append(_worker_heads(p, 0, len(p._memory_entries()))[1] + "\n".join([e["line"] for e in mem] + more))
    return "\n\n".join(parts)


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


def restart_block(p: Project, prev: dict) -> str:
    """The top of a run after a coordinator restart (preempt.request_restart): why the last run was stopped and
    the new instructions; "" when the last run was not restarted."""
    got = prev.get(preempt.RESTART_KEY)
    if not isinstance(got, dict):
        return ""
    at = got.get("at")
    when = f" at {timefmt.long(at, timefmt.home(p))}" if isinstance(at, (int, float)) else ""
    update = str(got.get("update") or "").strip()
    return (f"# RESTARTED BY THE COORDINATOR\nThe coordinator stopped your last run of this task{when} and "
            f"restarted it: {got.get('why') or 'no reason given'}\nThis costs no attempt. Your worktree, branch and "
            "commits are kept: check `git status` and `git log`, keep what still fits and change course as told. "
            "Detached jobs the last run started may still run: check them before starting new ones.\n"
            + (f"New instructions (they win over the spec below where they differ):\n{update}\n" if update else ""))


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
        short = (delta_review_task(p, task, cwd, branch, cmark, conflict["model_free"])
                 if isinstance(conflict.get("model_free"), dict) else conflict_fix_task(p, task, cwd, branch, cmark))
        return "\n\n".join(x for x in (worker_memory_block(p), worker_rules(p, task), short) if x.strip())
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
    if wake and run_tier != task["tier"] and not wake.get("escalated") and tierfloor.reasons(task, cfg):
        history += (f"This task ends in a conclusion the user reads, a verdict or a device or machine action, so the "
                    f"work it does when the wait is over is that: never do it at {run_tier}. Once the wait is over, "
                    f"hand off `waiting` with "
                    f"`retry_after_s: 0` and `wake_tier: \"{task['tier']}\"` instead.\n")
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
    history += outside_line(p, task)
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
    if kind in ("code", "review", "work", "harness"):
        history += remote_only_line(cfg)
    if kind == "review":
        history += no_commit_line(p, task)
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
        restart_block(p, prev),
        streamguard.prompt_block(prev),
        _sections(_raw(p, f"kind-{kind}.md"), cfg),
        worker_memory_block(p),
        worker_rules(p, task),
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
        f"{history}\n## Spec\n{specfold.for_prompt(p.state, task)}\n",
        replymsg.prompt_block(dict(task)),
        settled.prompt_block(p, dict(task)),
        taskfacts.prompt_block(p, dict(task)),
        overlap.prompt_block(p, dict(task))]
    parts.append(ctxblock.prompt_block(p, dict(task), "\n".join(parts), _system_memory(p)))
    parts.append(restrictions_pointer(p))
    return "\n\n".join(x for x in parts if x.strip())


def outside_line(p: Project, task: dict) -> str:
    """The task's last failed run, when no run since handed off: what it touched outside its folders
    (sidefx.py, its run dir's outside.json), or ''."""
    runs = p.db.q("SELECT id, dir FROM runs WHERE task=? AND role!='coordinator' AND status!='running' "
                  "ORDER BY id DESC LIMIT 10", (task["id"],))
    return sidefx.last_block([Path(r["dir"]) if r["dir"] else p.runs / str(r["id"]) for r in runs])


def _system_memory(p: Project) -> set[str]:
    """The memory entries worker_system shows (the context block leaves them out)."""
    try:
        return {e["name"] for e in worker_context(p, reserve=WORKER_RANKED_MIN_CHARS)[1]}
    except Exception:
        return set()


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
        "sides' intents. That resolution is the one edit you may make. No commit, intermediate ones included, "
        "may keep a conflict marker line.\n"
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


def delta_review_task(p: Project, task: dict, cwd: str, branch: str | None, cf: dict, mf: dict) -> str:
    """The short prompt of a light review of a model-free conflict resolution (conflictfix.model_free):
    the daemon rebased the approved head and kept both sides of each added hunk; the run judges only
    that resolution's delta and approves the resolved head, or fails it back to a standard fix."""
    head, onto, target = str(cf.get("head") or "?"), str(mf.get("onto") or "?"), str(cf.get("target") or "?")
    cb, sha = str(cf.get("branch") or ""), str(mf.get("sha") or "?")
    files = ", ".join(str(f) for f in mf.get("files") or []) or "?"
    return "\n\n".join([
        f"# YOUR TASK #{task['id']}: review a model-free push-conflict resolution for {task['title']}\n"
        f"kind: review · this run: light delta review (no review attempt) · budget ${task['budget_usd'] or 0:.2f} "
        f"(spent ${task['spent_usd'] or 0:.2f})\n"
        f"working directory: {cwd}" + (f" · branch: {branch}" if branch else "") + "\n"
        f"run dir: {RUN_DIR_MARK} ($TTP_RUN_DIR): the hand-off goes only to its result.json\n"
        f"project root: {p.root}\n" + _zone_line(p),
        f"This review already approved branch {cb or '(none)'} at {head}, but it conflicted with {target}. The "
        f"daemon rebased it onto {onto} without a model as {sha}, keeping both sides of each of the "
        f"{mf.get('hunks') or '?'} conflicting hunks where both only added lines (the tip's first), in {files}. "
        "The change itself was reviewed: judge only this resolution.\n\n"
        "Do only this:\n"
        f"1. Read the resolution's delta: `git range-diff $(git merge-base {head} {onto})..{head} {onto}..{sha}` "
        "(each commit against its rebased self) and the settled hunks below.\n"
        "2. Check each settled hunk keeps both sides' intents: no line or entry twice, nothing ordered so one "
        "side's lines land inside the other's block, no two versions of one setting.\n"
        f"3. Check out {sha} (detached) and run only the focused tests of the files above (a file, `-k` or "
        "`file::test`; never the full suite). The push queue runs the full push checks on the head you approve.\n"
        "4. Sound: hand off `done` with "
        f'"push": [{{"branch": "{cb}", "head": "{sha}"}}]. Edit nothing.\n\n'
        "Not sound, or the tests fail because of the resolution: hand off `failed` saying what it is. The "
        "conflict is then settled by a standard run at once, at no attempt.\n"
        "Never run `ttp push` or `git push`, never bump versions or write a changeset.\n\n"
        f"Settled hunks (`+<` the tip's lines, `+>` the change's):\n```\n{str(mf.get('delta') or '')}\n```",
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


def remote_only_line(cfg: dict) -> str:
    """On a workstation whose heavy checks go to spill hosts (checkspill.remote_only), the worker is told to run
    tests and builds only through `ttp checks`. "" elsewhere, or on error."""
    from . import checkspill
    try:
        if not checkspill.remote_only(cfg):
            return ""
    except Exception:
        return ""
    return ("\nThis machine is a workstation whose heavy checks go to spill hosts (delivery.checks_remote_only): run "
            "tests and builds only through `ttp checks`, never a plain pytest or build here.\n")


def no_commit_line(p: Project, task: dict) -> str:
    """A review that covers no commit (worktree.reviewed_head None, no extchange published head: its
    target is an external artifact or a hash-pinned report) is told `ttp checks` has no target there,
    so it never runs candidate checks at an unrelated HEAD. "" for a review of a commit, or on error."""
    from . import extchange
    from .push import _labels
    from .worktree import reviewed_head
    try:
        if extchange.heads(_labels(dict(task))) or reviewed_head(p, dict(task)):
            return ""
    except Exception:
        return ""
    return ("\nThis review covers no commit, so `ttp checks` and the push checks have no target: never run "
            "them at whatever HEAD a checkout has. Decide it on the spec's artifact hashes (re-hash each against "
            "its manifest; missing or changed fails) and focused reruns of the commands behind its claims "
            "(nonzero fails). A claim resting on code names that commit and runs `ttp checks` in its worktree. "
            "Such a failure is substantive, never check_only for a gate that does not apply.\n")


# An obligation's owner or milestone note (obligations._owner_note, _milestone_note) appended to a spec: up to
# the next `## ` heading that is not one, or the end.
_OBLIGATION_BLOCK = re.compile(r"\n*^#{2,3} Obligation .*?\n*(?=^## (?!Obligation )|\Z)", re.M | re.S)


def spec_digest(task: dict) -> str:
    """A short fingerprint of the task's spec, kept with each run, so a resumed session learns of a
    spec that changed while it was cut off. Appended `## Obligation` blocks are promise bookkeeping the
    run also gets through steer.md: they change no digest, so they never wake a waiting task. A spec
    without one keeps its digest byte for byte (obligations.attach strips the trailing space it follows)."""
    spec, cut = _OBLIGATION_BLOCK.subn("\n\n", str(task["spec"] or ""))
    spec = spec.rstrip() if cut else spec
    return hashlib.sha256(str(spec or task["title"]).encode()).hexdigest()[:16]


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
    if lost.get("cause") == "handoff":   # it ended without a hand-off the daemon takes (Daemon._handoff_resume)
        text = (f"# Continue task #{task['id']}: {task['title']}\n"
                f"Your run ended, but {lost.get('handoff')}\n"
                f"This continues your session once, in the same attempt (budget "
                f"${task['budget_usd'] or 0:.2f}, spent ${task['spent_usd'] or 0:.2f}). Do not redo finished work: "
                f"fix what this names and write the hand-off to $TTP_RUN_DIR/result.json as the Handoff section "
                f"says. Ending again without a valid one fails the run.\n")
        if lost.get("dir"):
            text += (f"This run has a new run directory, {RUN_DIR_MARK}: write the hand-off only there, not "
                     f"under {lost['dir']}.\n")
        if lost.get("spec_sha") and lost["spec_sha"] != spec_digest(task):
            text += ("\nUpdate for your task from the project coordinator. Where it differs from the spec, it wins:\n"
                     f"The spec changed:\n{specfold.for_prompt(p.state, task)}\n")
        return "\n\n".join(x for x in (text, restrictions_pointer(p)) if x.strip())
    why = {"reboot": "the host rebooted", "sleep": "the host slept",
           "network": "the network went away", "crash": "its process died without a result (a crash)"}.get(
        lost.get("cause"), "its supervisor was lost")
    free = lost.get("cause") in ("reboot", "sleep", "network", "crash")
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
        update.append(f"The spec changed:\n{specfold.for_prompt(p.state, task)}")
    steer = unread_update(Path(old_dir))[0] if old_dir else ""
    if steer:
        update.append(steer)
    if update:
        text += ("\nUpdate for your task from the project coordinator. Where it differs from the spec, it wins:\n"
                 + "\n".join(update) + "\n")
    return "\n\n".join(x for x in (text, restrictions_pointer(p)) if x.strip())


def worker_prompt(p: Project, task: dict, cwd: str, branch: str | None) -> str:
    """The whole prompt in one piece, as a worker reads it."""
    return worker_system(p) + "\n\n" + worker_task(p, task, cwd, branch)
