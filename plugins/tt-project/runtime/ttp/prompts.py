# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Worker prompts: harness rules + charter (restrictions are binding) + memory + the task.
Templates live in the project's own harness (prompts/*.md), so each project can tune them."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .db import continues_id, load_result
from .hook import unread_update
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


def _reboot_note(reboot) -> str:
    """What a run lost to a host reboot, or a wait from before one, must know before it redoes work."""
    if not isinstance(reboot, dict):
        return ""
    at = reboot.get("at")
    when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(at)) if isinstance(at, (int, float)) else "recently"
    out = (f"The host rebooted (booted {when}) since the last run; that does not count as an attempt. "
           "Detached jobs, /tmp files and device state from before the reboot are gone. Check `git status` "
           "and the job logs before redoing work.\n")
    notes = [str(x) for x in reboot.get("notes") or []]
    if notes:
        out += "The last run's notes:\n" + "".join(f"- {x}\n" for x in notes)
    return out


def worker_task(p: Project, task: dict, cwd: str, branch: str | None, wake: dict | None = None) -> str:
    """The per-task part of a worker's prompt, after worker_system(): the rules for its kind, the
    task itself, and the restrictions again at the end. `wake` (the daemon's run note) marks a run
    that wakes a waiting task, at a tier that may be below the task's own."""
    cfg = p.config()
    kind = task["kind"] or "work"
    history = ""
    prev = load_result(task["result"])
    if prev:
        history = f"\nPrevious attempt ended '{prev.get('status')}': {str(prev.get('summary') or '')[:1500]}\n"
        if prev.get("woke"):
            history += f"Woken because: {prev['woke']}.\n"
        history += _reboot_note(prev.get("reboot"))
    run_tier = (wake or {}).get("tier") or task["tier"]
    if wake and run_tier != task["tier"] and not wake.get("escalated"):
        history += (f"This run is a {run_tier} wake: check whether the wait is over. If it is and substantial "
                    f"work remains, hand off `waiting` with `retry_after_s: 0` and `wake_tier: \"{task['tier']}\"` "
                    f"at once: the task runs again now at its own tier, without costing an attempt.\n")
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
        f"kind: {kind} · tier: {task['tier']}" + (f" · this run: {run_tier} wake" if wake else "")
        + f" · attempt {int(task['attempts'] or 0) + 1} of "
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


def spec_digest(task: dict) -> str:
    """A short fingerprint of the task's spec, kept with each run, so a resumed session learns of a
    spec that changed while it was cut off."""
    return hashlib.sha256(str(task["spec"] or task["title"]).encode()).hexdigest()[:16]


def worker_resume(p: Project, task: dict, lost: dict) -> str:
    """The prompt that continues the agent session of a run the host took away (`lost`, from the
    daemon): what happened, what is gone, what changed since, then the restrictions again. The
    session already holds the task and its own work."""
    at = lost.get("ended")
    when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(at)) if isinstance(at, (int, float)) else "recently"
    why = {"reboot": "the host rebooted", "sleep": "the host slept"}.get(lost.get("cause"), "its supervisor was lost")
    free = lost.get("cause") in ("reboot", "sleep")
    text = (f"# Continue task #{task['id']}: {task['title']}\n"
            f"Your run was cut off at about {when}: {why}. This continues your session"
            + (" and does not count as an attempt" if free else "")
            + f" (attempt {int(task['attempts'] or 0) + 1} of {task['max_attempts']}, "
            f"budget ${task['budget_usd'] or 0:.2f}, spent ${task['spent_usd'] or 0:.2f}).\n"
            "Detached jobs, /tmp files and device state from before then are gone. Check `git status` and "
            "the job logs, then carry on where you left off; do not redo finished work.\n")
    old_dir = lost.get("dir")
    if old_dir:
        text += (f"This run has a new run directory: write the hand-off to $TTP_RUN_DIR/result.json "
                 f"(run `echo $TTP_RUN_DIR`), not under {old_dir}.\n")
    update = []
    if lost.get("spec_sha") and lost["spec_sha"] != spec_digest(task):
        update.append(f"The spec changed:\n{task['spec'] or task['title']}")
    steer = unread_update(Path(old_dir))[0] if old_dir else ""
    if steer:
        update.append(steer)
    if update:
        text += ("\nUpdate for your task from the project coordinator. Where it differs from the spec, it wins:\n"
                 + "\n".join(update) + "\n")
    return "\n\n".join(x for x in (text, restrictions_block(p)) if x.strip())


def worker_prompt(p: Project, task: dict, cwd: str, branch: str | None) -> str:
    """The whole prompt in one piece, as a worker reads it."""
    return worker_system(p) + "\n\n" + worker_task(p, task, cwd, branch)
