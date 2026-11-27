"""PR babysitting sweep, read-only: what the pr-watch watcher notices about every open PR the project owns,
draft or not, beyond failed checks and unanswered comments (those stay in watchers._check_findings).

Per PR, on each watcher run:
- `pr_ci_not_run` event: checks skipped, or workflows that need a manual or label trigger and never ran on the
  PR head. Each is listed with its trigger (workflow_dispatch, a label name, or none), read from the repo's
  .github/workflows at the head (REST, cached per head).
- `pr_idle` event: no commit, review, comment or check activity for `pr_babysit.idle_h` hours, with the
  requested reviewers. Once per idle stretch.
- status changes since the last sweep (merged or closed, out of draft, review approved or changes requested,
  CI red or green, a new conflict): one plain chat line per PR with its link, kept once per change. A review
  approved and red CI on a ready PR post at once; the rest join the chat digest. A first sighting is a baseline.
The sweep posts nothing to GitHub and runs no workflow. A gh call that gives no answer (exit 75: not in yet,
an error) gives no event: the next sweep asks again.
"""
from __future__ import annotations

import base64
import re
import time
from datetime import datetime

KEY = "pr_babysit"           # {pr_key: {"told": {condition: value}, "idle": mark, "notrun": mark}}
FLOWS_KEY = "pr_workflows"   # {"owner/repo@sha": {"at": ts, "flows": [{"name", "manual", "trigger"}]}}
FLOWS_RETRY_S = 3600
MAX_FLOWS = 40
READ_S = 900                 # a PR no task opened is read this often while the sweep is on
DEFAULTS = {"enabled": True, "idle_h": 24, "extra_prs": []}


def settings(cfg: dict | None) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("pr_babysit") or {})}


def _ts(s) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def workflow_info(path: str, text: str) -> dict:
    """{"name", "manual", "trigger"} of one workflow file, without a YAML parser: its `on:` block is the
    lines up to the next top-level key. `manual`: it runs only by dispatch or by a label."""
    name = re.search(r"^name:\s*(.+?)\s*$", text, re.M)
    block = re.search(r"^(?:on|'on'|\"on\"):(.*(?:\n(?:[ \t].*|#.*|\s*))*)", text, re.M)
    block = block.group(1) if block else ""
    dispatch = "workflow_dispatch" in block
    labels = []
    if "labeled" in block:
        labels = re.findall(r"github\.event\.label\.name\s*==\s*['\"]([^'\"]+)['\"]", text)
        labels += re.findall(r"contains\([^()]*labels[^()]*,\s*['\"]([^'\"]+)['\"]", text)
    trigger = " or ".join(["workflow_dispatch"] * dispatch + [f"label {x}" for x in dict.fromkeys(labels)]) or "none"
    manual = (labels and not re.search(r"opened|synchronize", block)) or (dispatch and not re.search(r"pull_request|push", block))
    return {"name": (name.group(1).strip("'\"") if name else path.rsplit("/", 1)[-1].rsplit(".", 1)[0]),
            "manual": bool(manual), "trigger": trigger}


def _flows(db, gh, repo: str, sha: str, cwd: str, now: float) -> list[dict] | None:
    """The repo's workflows at `sha`, cached per head; None when gh gave no answer (retried hourly)."""
    cache = db.kv(FLOWS_KEY, {}) or {}
    rec = cache.get(f"{repo}@{sha}")
    if rec and (rec.get("flows") is not None or now - float(rec.get("at") or 0) < FLOWS_RETRY_S):
        return rec.get("flows")
    listing = gh(["api", f"repos/{repo}/contents/.github/workflows?ref={sha}"], cwd)
    flows = None
    if isinstance(listing, list):
        flows = []
        for f in [f for f in listing if str(f.get("name", "")).endswith((".yml", ".yaml"))][:MAX_FLOWS]:
            one = gh(["api", f"repos/{repo}/contents/{f['path']}?ref={sha}"], cwd)
            if not isinstance(one, dict) or not one.get("content"):
                flows = None
                break
            flows.append(workflow_info(f["path"], base64.b64decode(one["content"]).decode("utf-8", "replace")))
    elif listing is not None:
        flows = []
    cache = {k: v for k, v in cache.items() if now - float(v.get("at") or 0) < 7 * 86400}
    db.set_kv(FLOWS_KEY, {**cache, f"{repo}@{sha}": {"at": now, "flows": flows}})
    return flows


def _workflow(c: dict) -> str:
    flow = ((c.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {}
    return str(c.get("workflowName") or flow.get("name") or "")


def _not_run(daemon, pr: dict, key: str, gh, now: float) -> str:
    """The `pr_ci_not_run` text: skipped checks and manual workflows that never ran on the head."""
    sha, repo = pr.get("headRefOid"), key.split("#")[0]
    checks = pr.get("statusCheckRollup") or []
    flows = _flows(daemon.p.db, gh, repo, sha, str(daemon.p.root), now) if sha else None
    if flows is None:
        return ""
    by_name = {f["name"]: f["trigger"] for f in flows}
    ran = {_workflow(c) for c in checks}
    skipped = {_workflow(c) or c.get("name") or "?" for c in checks if (c.get("conclusion") or "").upper() == "SKIPPED"}
    todo = {n: by_name.get(n, "none") for n in skipped}
    todo.update({f["name"]: f["trigger"] for f in flows if f["manual"] and f["name"] not in ran})
    return "; ".join(f"{n} (trigger: {t})" for n, t in sorted(todo.items()))


def _told(daemon, state: dict, key: str, url: str, sig: dict, ready: bool) -> None:
    """One chat line per status change since the last sweep, kept per PR and condition."""
    from . import chatdigest
    told = state.setdefault("told", {})
    now = {"state": sig["state"], "draft": sig["draft"], "decision": sig["decision"] or "",
           "conflict": sig["mergeable"] == "CONFLICTING"}
    if sig["checks"] in ("failing", "passing"):
        now["ci"] = sig["checks"]
    for cond, value in now.items():
        old = told.get(cond)
        told[cond] = value
        if old is None or old == value:
            continue
        line = {("state", "MERGED"): "merged", ("state", "CLOSED"): "closed", ("draft", False): "out of draft",
                ("decision", "APPROVED"): "review approved", ("decision", "CHANGES_REQUESTED"): "changes requested",
                ("ci", "failing"): "CI red", ("ci", "passing"): "CI green", ("conflict", True): "has a merge conflict"
                }.get((cond, value))
        if line:
            at_once = line == "review approved" or (line == "CI red" and ready)
            chatdigest.routine(daemon.p, None, f"[{key}]({url}): {line}", f"pr status {key} {cond}", at_once=at_once)


def sweep(daemon, t: dict, pr: dict, sig: dict, gh) -> None:
    """Run the sweep on one PR the watcher just read; `gh` is the watcher's gh caller (route, deadline)."""
    cfg = settings(getattr(daemon, "cfg", None))
    url = pr.get("url") or t["pr_url"]
    key = url.split("github.com/", 1)[-1].replace("/pull/", "#")
    db, now = daemon.p.db, time.time()
    allstate = dict(db.kv(KEY, {}) or {})
    state = allstate.setdefault(key, {})
    _told(daemon, state, key, url, sig, pr.get("isDraft") is False)
    events = []
    if sig["state"] == "OPEN" and sig["checks"] != "pending":
        text = _not_run(daemon, pr, key, gh, now)
        if text and state.get("notrun") != (mark := f"{pr.get('headRefOid')} {text}"):
            state["notrun"] = mark
            events.append(("pr_ci_not_run", f"PR {url}: checks skipped or never run on the head: {text}. Dispatch "
                           "or label what this change needs (the project's own PRs only)."))
        last = max([_ts(pr.get("updatedAt"))] + [_ts(c.get(k)) for c in pr.get("statusCheckRollup") or []
                                                 for k in ("completedAt", "startedAt")])
        hours = (now - last) / 3600 if last else 0
        if last and hours >= float(cfg["idle_h"]) and state.get("idle") != last:
            state["idle"] = last
            who = [r.get("login") or r.get("name") for r in pr.get("reviewRequests") or []]
            events.append(("pr_idle", f"PR {url} idle for {hours:.0f} h (no commit, review, comment or check "
                           f"activity). Requested reviewers: {', '.join(x for x in who if x) or 'none'}."))
    for kind, text in events:
        from . import screen as scr
        status = "muted" if scr.count_muted(db, "pr", text, "normal") else "queued"
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "pr", kind, "normal", text, status, t.get("id")))
    db.set_kv(KEY, allstate)
