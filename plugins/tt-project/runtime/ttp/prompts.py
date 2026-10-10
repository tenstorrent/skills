# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Worker prompts: harness rules + charter (restrictions are binding) + memory + the task.
Templates live in the project's own harness (prompts/*.md), so each project can tune them."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import budget as bud
from . import timefmt
from .db import continues_id, load_result
from .hook import unread_update
from .project import WORKER_MEMORY_CHARS, Project, code_tasks_may_push, push_queue_on
from .worktree import carried_branch, gets_worktree, own_worktree, project_venv


def _read(p: Project, name: str) -> str:
    f = p.harness / "prompts" / name
    return strip_markers(f.read_text()) if f.exists() else ""


def strip_markers(text: str) -> str:
    """`text` without its `<!-- ttp:... -->` / `<!-- /ttp:... -->` lines. They fence a block (such as
    kind-review.md's result rule) so a project's own wording of it merges as one unit on upgrades;
    the model never needs them."""
    return "".join(line for line in text.splitlines(keepends=True)
                   if not (line.startswith(("<!-- ttp:", "<!-- /ttp:")) and line.rstrip().endswith("-->")))


# kind-review.md's two delivery sections; a review's prompt carries only the one its project uses.
REVIEW_PUSH_SECTIONS = ("## Pushing a reviewed change", "## Approving into the push queue")


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


def worker_system(p: Project) -> str:
    """The part of a worker's prompt shared by every task, whatever its kind or tier, until the
    charter or memory changes: sent as the system prompt where the agent allows it, so the provider
    caches it across all the project's workers. Nothing about the task may go in here."""
    parts = [restrictions_block(p), _read(p, "worker.md")]
    # The restrictions open and close the prompt; a third copy inside the charter only costs tokens.
    charter = p.charter_path.read_text() if p.charter_path.exists() else "(none)"
    parts.append("# CHARTER (goals and policies; its restrictions are the binding block above)\n" +
                 charter_without_restrictions(charter_without_placeholders(charter)))
    mem = p.memory_text(limit_chars=WORKER_MEMORY_CHARS)
    if mem:
        parts.append("# PROJECT MEMORY\n" + mem)
    return "\n\n".join(x for x in parts if x.strip())


def _zone_line(p: Project, now: float | None = None) -> str:
    """The project's home zone and the time there now, for the task header."""
    tz = timefmt.home(p)
    return (f"home time zone: {tz}, now {timefmt.long(time.time() if now is None else now, tz)}: write times "
            f"for the user in it\n")


def _reboot_note(reboot, tz: str | None = None) -> str:
    """What a run lost to a host reboot, or a wait from before one, must know before it redoes work."""
    if not isinstance(reboot, dict):
        return ""
    at = reboot.get("at")
    when = (timefmt.long(at, tz) if tz else time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(at))) \
        if isinstance(at, (int, float)) else "recently"
    out = (f"The host rebooted (booted {when}) since the last run; that does not count as an attempt. "
           "Detached jobs, /tmp files and device state from before the reboot are gone. Check `git status` "
           "and the job logs before redoing work.\n")
    notes = [str(x) for x in reboot.get("notes") or []]
    if notes:
        out += "The last run's notes:\n" + "".join(f"- {x}\n" for x in notes)
    return out


# Stands in a worker prompt for its run's own directory, which exists only once the run is recorded:
# start_run puts the path in.
RUN_DIR_MARK = "<this run's dir>"


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
    old_id = continues_id(task)
    old = p.db.task(old_id) if old_id else None
    if old:
        on = f" on branch {old['branch']}" if own_worktree(old) and old["branch"] else ""
        was = str(load_result(old["result"]).get("summary") or old["blocked_reason"] or "")[:1500]
        history += (f"\nThis task continues #{old['id']} {old['title']} ({old['status']}){on}"
                    + (", and starts from that branch's head" if on and (branch or gets_worktree(p, kind)) else "")
                    + (", and starts from the head of what it reviewed" if old["kind"] == "review" and branch else "")
                    + (f". Its last summary: {was}" if was else "") + "\n")
    if (onto := carried_branch(task)) and branch:
        history += (f"\nThis task delivers onto branch {onto} (`ttp push --own --detach`, fast-forward only)"
                    + (": its worktree is on it.\n" if branch == onto else
                       f": it could not be checked out here, so this task works on its own branch, from that "
                       f"branch's head when there is one, and the push publishes its head onto {onto}.\n"))
    delivery = cfg.get("delivery", {})
    venv = project_venv(p, cwd)
    venv_line = (f"python venv: {venv} (the project's, already active: VIRTUAL_ENV and PATH; shared with other "
                 "workers, so do not install into it; its editable installs import the project root's code, not "
                 "yours; need other packages? make a venv of your own in the working directory)\n") if venv else ""
    parts = [
        drop_section(_read(p, f"kind-{kind}.md"), REVIEW_PUSH_SECTIONS[0 if push_queue_on(cfg) else 1]),
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
        + f"project root: {p.root}\n"
        + _zone_line(p)
        + venv_line
        + f"delivery policy: draft PRs={delivery.get('draft_prs', True)}, review before PR="
        f"{delivery.get('review_before_pr', True)}, auto-merge repos={delivery.get('auto_merge_repos') or 'none'}, "
        f"push allowed={delivery.get('push_allowed', True)}"
        + (f", code tasks may land on {delivery['push_branch']} with ttp push=True"
           if kind == "code" and code_tasks_may_push(cfg) else "")
        + (", push queue=True" if kind == "review" and push_queue_on(cfg) else "") + "\n"
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
    when = timefmt.long(at, timefmt.home(p)) if isinstance(at, (int, float)) else "recently"
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
    return "\n\n".join(x for x in (text, restrictions_block(p)) if x.strip())


def worker_prompt(p: Project, task: dict, cwd: str, branch: str | None) -> str:
    """The whole prompt in one piece, as a worker reads it."""
    return worker_system(p) + "\n\n" + worker_task(p, task, cwd, branch)
