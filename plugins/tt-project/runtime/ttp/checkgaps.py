# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One repair owner per missing check signature, with model-free retries on config changes."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import push, worktree
from .db import dump_result, load_result
from .project import write_json

KEY = "check_applicability_gaps"
FAILURE = "check_failure.json"
MAX_OWNERS = 3
FINISHED = ("done", "failed", "cancelled")


def config_sig(cfg: dict) -> str:
    checks = push.checks_config(cfg.get("delivery") or {})
    return hashlib.sha256(json.dumps(checks, sort_keys=True).encode()).hexdigest()


def probe(p, sig: str, config: str) -> str:
    """Each waiter is released by a change from the config it failed under, not the gap's first."""
    return f"{push._own_ttp(p)} checks --gap {sig} --gap-config {config}"


def _queue_owner(p, key: str, gap: dict, cfg: dict) -> int | None:
    """One live owner per signature. Past MAX_OWNERS, one coordinator event instead of another task."""
    owners = gap.setdefault("owners", [gap["owner"]] if gap.get("owner") else [])
    if len(owners) >= MAX_OWNERS:
        if not gap.get("escalated"):
            gap["escalated"] = time.time()
            waiters = ", ".join("#" + t for t in gap["tasks"]) or "none"
            p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                   (time.time(), "daemon", "check_gap_unowned", f"check_gap_unowned:{key}", "normal",
                    f"{key} ({gap['check']}) is still unrepaired after {len(owners)} repair owners "
                    f"({', '.join('#' + str(o) for o in owners)}); waiting: {waiters}. Decide the fix for "
                    "delivery.push_checks or reassign it; no further owner is queued.", "queued"))
        return None
    owner = p.db.add_task(
        f"Repair check applicability {key.removeprefix('check:')}",
        "Fix this project's own delivery.push_checks in its harness config. "
        f"Check signature {key}; command {gap['check']!r}; {gap.get('detail', 'missing target')}. "
        "Reproduce at the branch tip. Remove stale SHA pins or temporary environment paths and "
        "declare the actual scope with if_exists/if_changed, retaining mandatory coverage for "
        "related changes. Do not edit another project's harness or disable checks wholesale. "
        "For an unmatched mandatory selector, add coverage for the tested head without weakening the "
        "selector or any original mandatory check. Never add a harness-preparation head to a "
        "publication whitelist; qualify it with `ttp checks --purpose harness` through a reviewed "
        "harness definition. Never approve from byte equivalence or a "
        "previous head's pass. Commit the config repair; the daemon reruns dependent checks "
        "without a model when it changes."
        + (f" Earlier owners {', '.join('#' + str(o) for o in owners)} ended without a working repair."
           if owners else ""),
        kind="harness", origin="daemon", labels=[key], priority=2)
    owners.append(owner)
    gap["owner"], gap["owner_config"] = owner, config_sig(cfg)
    return owner


def _owner_dead(p, gap: dict, current: str, failed_again: bool) -> bool:
    """Failed, cancelled or missing; done counts too once its config left the check failing."""
    if gap.get("escalated"):
        return False
    task = p.db.task(int(gap["owner"])) if gap.get("owner") else None
    if not task or task["status"] in ("failed", "cancelled"):
        return True
    return task["status"] == "done" and (failed_again or gap.get("owner_config", gap["config"]) == current)


def _waiting(p, key: str, tid: str) -> dict | None:
    task = p.db.task(int(tid))
    if not task or task["status"] in FINISHED or load_result(task["result"]).get("check_gap") != key:
        return None
    return task


def _renamed(p, key: str, gap: dict) -> None:
    """Waiters' summaries name the owner actually working on the gap."""
    for tid in gap["tasks"]:
        task = _waiting(p, key, tid)
        if task:
            prev = load_result(task["result"])
            summary = re.sub(rf"Waiting on {re.escape(key)}, owner #\d+\.", _owned(key, gap), str(prev.get("summary") or ""))
            p.db.update_task(task["id"], result=dump_result({**prev, "summary": summary}))


def _owned(key: str, gap: dict) -> str:
    if gap.get("escalated"):
        return f"Waiting on {key}, owner #{gap['owner']}. Raised to the coordinator."
    return f"Waiting on {key}, owner #{gap['owner']}."


def wait(p, task: dict, result: dict, run_dir: Path, cfg: dict) -> dict | None:
    """A check-only failure with tree evidence becomes a wait, never an approval. Unknown or
    related scope and mixed/code findings retain the ordinary failure path."""
    if result.get("review_failure") != "check_only" or result.get("status") not in ("failed", "blocked"):
        return None
    try:
        receipt = json.loads((run_dir / FAILURE).read_text())
        if receipt.get("purpose", "candidate") != "candidate" or not receipt.get("check"):
            return None   # a harness run opens no check gap; a run that failed no check has none to own
        check = push.check_list([receipt["check"]])[0]
        path = Path(receipt["worktree"])
        head = receipt["head"]
        # Only this project's code worktrees; never act on another project's harness or state.
        if path.resolve() != p.root.resolve() and not path.resolve().is_relative_to(p.worktrees.resolve()):
            return None
        if check.config() not in [c.config() for c in push.check_list(push.repo_checks(cfg.get("delivery") or {}, path, p)[0])]:
            return None
        if worktree._git(path, "rev-parse", "HEAD") != head:
            return None
        failure = push.applicability_failure(path, head, check, receipt["rc"], receipt["output"], receipt.get("base", ""))
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return None
    if not failure or not failure["unrelated"]:
        return None
    sig, now = failure["sig"], time.time()
    key = "check:" + sig
    with p.db.tx():
        gaps = p.db.kv(KEY, {})
        gap = gaps.get(key)
        current = config_sig(cfg)
        if not gap:
            gap = {"check": check.config(), "config": current, "created": now, "tasks": {},
                   "detail": f"{failure['kind']}: {', '.join(failure['targets'])}"}
            gaps[key] = gap
            _queue_owner(p, key, gap, cfg)
        elif _owner_dead(p, gap, current, failed_again=True):
            if _queue_owner(p, key, gap, cfg) or gap.get("escalated"):
                _renamed(p, key, gap)
        # The gap's fingerprint follows its newest waiter; release is still judged per waiter.
        gap["config"] = current
        try:
            record = json.loads((run_dir / push.RECORDED).read_text())
            own = {str(c) for c in push.check_list(push.repo_checks(cfg.get("delivery") or {}, path, p)[0])}
            extras = [str(c) for c in record.get("commands", []) if str(c) not in own]
        except (OSError, ValueError, TypeError, AttributeError):
            extras = []
        gap["tasks"][str(task["id"])] = {"run_dir": str(run_dir), "worktree": str(path), "head": head,
                                        "config": current, "extras": extras}
        p.db.set_kv(KEY, gaps)
    return {**result, "status": "waiting", "summary": str(result.get("summary") or "") + " " + _owned(key, gap),
            "waiting_for": key, "check_gap": key, "retry_when": probe(p, sig, current),
            "wait_owner": "coordinator" if gap.get("escalated") else f"task:{gap.get('owner')}",
            "retry_after_s": 1800, "wake_tier": "standard", "wait_kind": "external"}


def ready(p, sig: str, since: str | None = None) -> bool:
    """Read-only probe: a config change since the waiter's own failure is the release condition,
    not an owner's claimed success."""
    current = config_sig(p.config())
    if since:
        return current != since
    gap = p.db.kv(KEY, {}).get("check:" + sig)
    return not gap or gap["config"] != current


def rerun(daemon) -> None:
    """Retarget each waiting hand-off to detached checks on its unchanged, clean tree. Config
    fingerprints are durable per task, so another tick or daemon restart does not duplicate work."""
    p, cfg = daemon.p, daemon.cfg
    gaps = p.db.kv(KEY, {})
    current = config_sig(cfg)
    configured = [c.config() for c in push.all_checks(cfg.get("delivery") or {})]
    for key, gap in list(gaps.items()):
        live = [tid for tid in gap["tasks"] if _waiting(p, key, tid)]
        if gap["check"] not in configured and not live:
            # The check left push_checks and nobody waits on it: the gap is over.
            with p.db.tx():
                gaps.pop(key)
                p.db.set_kv(KEY, gaps)
            continue
        if live and _owner_dead(p, gap, current, failed_again=False):
            with p.db.tx():
                if _queue_owner(p, key, gap, cfg) or gap.get("escalated"):
                    p.db.set_kv(KEY, gaps)
                    _renamed(p, key, gap)
        for tid, member in gap["tasks"].items():
            task = p.db.task(int(tid))
            prev = load_result(task["result"]) if task else {}
            if not task or task["status"] != "queued" or prev.get("check_gap") != key or member["config"] == current:
                continue
            path, out = Path(member["worktree"]), Path(member["run_dir"])
            try:
                clean = (worktree._git(path, "rev-parse", "HEAD", check=False) == member["head"] and not
                         worktree._git(path, "status", "--porcelain", "--untracked-files=no", check=False))
            except (OSError, subprocess.SubprocessError):
                clean = False
            if not clean or prev.get("retry_when") != probe(p, key.removeprefix("check:"), member["config"]):
                # Changed/deleted worktrees wake normally; already-started checks keep their probe.
                continue
            # Save before starting: a restart sees the new probe and adopts the detached checks.
            result_probe = f"{push._own_ttp(p)} checks --result {shlex.quote(str(out))}"
            waiting = {**prev, "retry_when": result_probe, "waiting_for": "checks after applicability repair",
                       "wait_kind": "self"}
            write_json(out / "result.json", waiting)
            p.db.update_task(task["id"], result=dump_result(waiting), not_before=time.time() + 1800)
            previous_config = member["config"]
            member["config"] = current
            p.db.set_kv(KEY, gaps)
            extra = ["--fresh"] + (["--", *member["extras"]] if member.get("extras") else [])
            if not daemon._start_checks(path, out, f"check-gap-t{tid}", task["id"], extra):
                member["config"] = previous_config
                p.db.set_kv(KEY, gaps)
                write_json(out / "result.json", prev)
                p.db.update_task(task["id"], result=dump_result(prev))
