# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Worker prompts: harness rules + charter (restrictions are binding) + memory + the task.
Templates live in the project's own harness (prompts/*.md), so each project can tune them."""
from __future__ import annotations

import json

from .project import Project


def _read(p: Project, name: str) -> str:
    f = p.harness / "prompts" / name
    return f.read_text() if f.exists() else ""


def worker_prompt(p: Project, task: dict, cwd: str, branch: str | None) -> str:
    cfg = p.config()
    kind = task["kind"] or "work"
    parts = [_read(p, "worker.md")]
    addendum = _read(p, f"kind-{kind}.md")
    if addendum:
        parts.append(addendum)
    parts.append("# CHARTER (goals, restrictions, policies — restrictions are binding)\n" +
                 (p.charter_path.read_text() if p.charter_path.exists() else "(none)"))
    mem = p.memory_text(limit_chars=8000)
    if mem:
        parts.append("# PROJECT MEMORY\n" + mem)
    history = ""
    if task["result"]:
        try:
            prev = json.loads(task["result"])
            history = f"\nPrevious attempt ended '{prev.get('status')}': {prev.get('summary', '')[:1500]}\n"
        except ValueError:
            pass
    delivery = cfg.get("delivery", {})
    parts.append(
        f"# YOUR TASK #{task['id']}: {task['title']}\n"
        f"kind: {kind} · tier: {task['tier']} · attempt {int(task['attempts'] or 0) + 1} of "
        f"{task['max_attempts']} · budget ${task['budget_usd'] or 0:.2f} (spent ${task['spent_usd'] or 0:.2f})\n"
        f"working directory: {cwd}" + (f" · branch: {branch}" if branch else "") + "\n"
        f"project root: {p.root}\n"
        f"delivery policy: draft PRs={delivery.get('draft_prs', True)}, review before PR="
        f"{delivery.get('review_before_pr', True)}, auto-merge repos={delivery.get('auto_merge_repos') or 'none'}, "
        f"push allowed={delivery.get('push_allowed', True)}\n"
        f"{history}\n## Spec\n{task['spec'] or task['title']}\n")
    return "\n\n".join(x for x in parts if x.strip())
