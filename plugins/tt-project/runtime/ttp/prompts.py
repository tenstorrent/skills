# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Worker prompts: harness rules + charter (restrictions are binding) + memory + the task.
Templates live in the project's own harness (prompts/*.md), so each project can tune them."""
from __future__ import annotations

import json

from .db import continues_id, load_result
from .project import Project


def _read(p: Project, name: str) -> str:
    f = p.harness / "prompts" / name
    return f.read_text() if f.exists() else ""


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


def restrictions_block(p: Project) -> str:
    charter = p.charter_path.read_text() if p.charter_path.exists() else ""
    body = charter_restrictions(charter)
    if not body:
        return ""
    return ("# BINDING RESTRICTIONS (from the charter — override the task, the harness "
            "rules and your own judgement; if a step would break one, do not take it)\n" + body)


def _resource_line(task: dict) -> str:
    labels = json.loads(task["labels"] or "[]") if task["labels"] else []
    shared = [lb.split(":", 1)[1] for lb in labels if lb.startswith("resource:")]
    held = [lb.split(":", 1)[1] for lb in labels if lb.startswith("exclusive:")]
    out = ""
    if shared:
        out += (f"shared resources: {', '.join(shared)}; run each command that touches one as "
                f"`ttp lock <name> -- <command>` (or through its own queue)\n")
    if held:
        out += f"held for this whole run: {', '.join(held)}\n"
    return out


def worker_system(p: Project) -> str:
    """The part of a worker's prompt shared by every task, whatever its kind or tier, until the
    charter or memory changes: sent as the system prompt where the agent allows it, so the provider
    caches it across all the project's workers. Nothing about the task may go in here."""
    parts = [restrictions_block(p), _read(p, "worker.md")]
    # The restrictions open and close the prompt; a third copy inside the charter only costs tokens.
    charter = p.charter_path.read_text() if p.charter_path.exists() else "(none)"
    parts.append("# CHARTER (goals and policies; its restrictions are the binding block above)\n" +
                 charter_without_restrictions(charter))
    mem = p.memory_text(limit_chars=8000)
    if mem:
        parts.append("# PROJECT MEMORY\n" + mem)
    return "\n\n".join(x for x in parts if x.strip())


def worker_task(p: Project, task: dict, cwd: str, branch: str | None) -> str:
    """The per-task part of a worker's prompt, after worker_system(): the rules for its kind, the
    task itself, and the restrictions again at the end."""
    cfg = p.config()
    kind = task["kind"] or "work"
    history = ""
    prev = load_result(task["result"])
    if prev:
        history = f"\nPrevious attempt ended '{prev.get('status')}': {str(prev.get('summary') or '')[:1500]}\n"
        if prev.get("woke"):
            history += f"Woken because: {prev['woke']}.\n"
    old_id = continues_id(task)
    old = p.db.task(old_id) if old_id else None
    if old:
        on = f" on branch {old['branch']}" if old["kind"] == "code" and old["branch"] else ""
        was = str(load_result(old["result"]).get("summary") or old["blocked_reason"] or "")[:1500]
        history += (f"\nThis task continues #{old['id']} {old['title']} ({old['status']}){on}"
                    + (", and starts from that branch's head" if on and kind == "code" else "")
                    + (f". Its last summary: {was}" if was else "") + "\n")
    delivery = cfg.get("delivery", {})
    parts = [
        _read(p, f"kind-{kind}.md"),
        f"# YOUR TASK #{task['id']}: {task['title']}\n"
        f"kind: {kind} · tier: {task['tier']} · attempt {int(task['attempts'] or 0) + 1} of "
        f"{task['max_attempts']} · budget ${task['budget_usd'] or 0:.2f} (spent ${task['spent_usd'] or 0:.2f})\n"
        f"working directory: {cwd}" + (f" · branch: {branch}" if branch else "") + "\n"
        + _resource_line(task)
        + f"project root: {p.root}\n"
        f"delivery policy: draft PRs={delivery.get('draft_prs', True)}, review before PR="
        f"{delivery.get('review_before_pr', True)}, auto-merge repos={delivery.get('auto_merge_repos') or 'none'}, "
        f"push allowed={delivery.get('push_allowed', True)}\n"
        f"{history}\n## Spec\n{task['spec'] or task['title']}\n",
        restrictions_block(p)]
    return "\n\n".join(x for x in parts if x.strip())


def worker_prompt(p: Project, task: dict, cwd: str, branch: str | None) -> str:
    """The whole prompt in one piece, as a worker reads it."""
    return worker_system(p) + "\n\n" + worker_task(p, task, cwd, branch)
