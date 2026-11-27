"""PR babysitting sweep, read-only: what the pr-watch watcher notices about every open PR the project owns,
draft or not, beyond failed checks and unanswered comments (those stay in watchers._check_findings).

Per PR, on each watcher run:
- `pr_ci_not_run` event: checks skipped, or workflows that need a manual or label trigger and never ran on the
  PR head and that `pr_babysit.dispatch` lists for the repo. Each is listed with its trigger (workflow_dispatch,
  a label name, or none), read from the repo's .github/workflows at the head (REST, each file kept by its blob).
- `pr_idle` event: no commit, review, comment or check activity for `pr_babysit.idle_h` hours, with the
  requested reviewers. Once per idle stretch.
- status changes since the last sweep (merged or closed, out of draft, review approved or changes requested,
  CI red or green, a new conflict): one plain chat line per PR with its link, kept once per change. A review
  approved and red CI on a ready PR post at once; the rest join the chat digest. A first sighting is a baseline.
Once per run, not per PR:
- `pr_route_silent` event: the route PRs are read through (delivery.gh_route) has not answered for over 24 h,
  naming the route, how long it has been silent and the user's own step to bring it back; once per silence,
  clearing once the route answers.
- `pr_overlap` event: two open PRs in the same repo, at least one the project owns, that change the same
  files, naming the files and both PRs, once per pair, so the two projects coordinate. Every project's sweep
  records its open PRs and their changed files for the host's other projects' sweeps to read.
The sweep posts nothing to GitHub and runs no workflow. A gh call that gives no answer (exit 75: not in yet,
an error) gives no event: the next sweep asks again.
"""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime

from . import project

KEY = "pr_babysit"           # {pr_key: {"told": {condition: value}, "idle": mark, "notrun": mark, "branch", "open", "head"}}
FLOWS_KEY = "pr_workflows"   # {"owner/repo:path:blob": {"at": ts, "info": {"name", "file", "manual", "trigger", "labels"}}
READ_S = 900                 # a PR no task opened is read this often while the sweep is on
ROUTE_SILENT_S = 24 * 3600   # a route silent longer than this makes the sweep raise pr_route_silent
ROUTE_KEY = "pr_route"       # kv: {"via", "at"} of the silence already told
SHOWN_FILES = 5             # how many shared files an overlap event names
OVERLAP_KEY = "pr_overlap"   # kv: the overlapping PR pairs already told: {f"{a}|{b}": 1}
FILES_KEY = "pr_files"       # kv: {key: {"head": sha, "files": [name, ...]}}
HOST_FILE = "pr-overlap.json"   # under the tt-project home: {project: {"at": ts, "prs": {key: {"head", "files"}}}}
HOST_STALE_S = 48 * 3600     # a host project's record older than this is dropped when the file is read
DEFAULTS = project.DEFAULT_CONFIG["pr_babysit"]
PING = "This PR has had no recent activity: a review or a reply would unblock it. Thank you."


def settings(cfg: dict | None) -> dict:
    out = {**DEFAULTS, **((cfg or {}).get("pr_babysit") or {})}
    return {**out, "dispatch": {str(k).lower(): v for k, v in (out.get("dispatch") or {}).items()}}


def owned(db) -> dict[str, str]:
    """{"owner/repo#N": head branch} of the open PRs the sweep reads, the ones this project owns."""
    return {k: v["branch"] for k, v in (db.kv(KEY, {}) or {}).items() if v.get("open") and v.get("branch")}


def _target(real: str, more: list[str]) -> str:
    """The "owner/repo#N" a `gh pr/issue <sub> [N] [-R repo]` call names, or ""."""
    from . import prguard
    pos = prguard._positionals(more, ("-R", "--repo", "--add-label", "--remove-label", "-t", "--title", "-b", "--body",
                                      "-F", "--body-file", "-c", "--comment", "-m", "--message"))
    repo = (prguard._opts(more, ("-R", "--repo")) or [None])[-1]
    if pos[:1] and pos[0].isdigit() and repo:
        return f"{repo}#{pos[0]}".lower()
    out = pos[0] if pos[:1] and pos[0].startswith("http") else prguard._gh(
        real, "pr", "view", *pos[:1], *(["-R", repo] if repo else []), "--json", "url", "-q", ".url")
    return prguard.pr_key(out) or ""


def guard(args: list[str], real: str, db, cfg: dict, stdin_text: str | None = None) -> str | None:
    """Why `gh args` must not run for this project's PR babysitting rules, or None: a PR in
    pr_babysit.no_post_prs gets no post, edit or dispatch; a workflow dispatch (`workflow run <name> --ref
    <head>`) or a label (`pr edit N --add-label L`) goes only on an open PR the project owns and only as
    pr_babysit.dispatch lists it for the repo ('<workflow>' or 'label:<name>'); a dispatch carries no inputs."""
    from . import prbody, prguard
    cmd, sub, more = args[0], (args[1] if len(args) > 1 else ""), args[2:]
    cfg_ = settings(cfg)
    no_post = {k.lower() for k in cfg_["no_post_prs"]}
    if cmd == "workflow" and sub == "run":
        from . import ghqueue
        pos, opts, bad = ghqueue._parse(more, ghqueue._DISPATCH_FLAGS)
        if bad or len(pos) != 1 or len(opts.get("--ref", [])) != 1 or any(len(v) > 1 for v in opts.values()):
            return ("refused: `gh workflow run` runs one named workflow with --ref <the PR's head branch>, and "
                    f"no inputs or other options ({bad or 'form'})")
        repo = (opts.get("--repo") or [prguard._gh(real, "repo", "view", "--json", "nameWithOwner", "-q",
                                                   ".nameWithOwner").strip()])[0]
        ref, what = opts["--ref"][0], pos[0]
        key = next((k for k, b in owned(db).items() if k.startswith(f"{repo.lower()}#") and b == ref), "") if db else ""
        if not key:
            return f"refused: {ref!r} is not the head of an open PR this project owns in {repo}"
        if key.lower() in no_post:
            return f"refused: {key} is in pr_babysit.no_post_prs: nothing is dispatched or posted on it"
        if what not in cfg_["dispatch"].get(repo.lower(), []):
            return f"refused: workflow {what!r} is not listed for {repo} in pr_babysit.dispatch"
        return None
    if cmd == "api":   # the same refusals when a write goes through the REST or GraphQL endpoint
        endpoint, method = prguard._api_target(args[1:])
        m = re.match(r"repos/([^/]+/[^/]+)/(?:issues|pulls)/(\d+)", endpoint, re.I)
        if method != "GET" and re.search(r"/actions/workflows/[^/]+/dispatches$", endpoint, re.I):
            return "refused: dispatch a workflow with `gh workflow run <name> --ref <the PR's head>`"
        if method != "GET" and (re.search(r"/(?:issues|pulls)/\d+/labels", endpoint, re.I) or _sets_labels(args[1:], stdin_text)):
            return "refused: add a label with `gh pr edit <N> --add-label <label>` on an owned PR"
        mutation = endpoint.strip("/").lower() == "graphql" and prbody.GQL_MUTATION_RE.search(prguard._api_text(args[1:], stdin_text))
        if method != "GET" and (m and f"{m[1]}#{m[2]}".lower() in no_post or no_post and mutation):
            return ("refused: a write on a PR in pr_babysit.no_post_prs, or a GraphQL mutation whose target cannot be "
                    "checked while any PR is listed there: fix it by local commits and tell the user")
        return None
    if cmd in ("pr", "issue") and sub == "create" and prguard._opts(more, ("-l", "--label")):
        return "refused: a label goes on an owned PR with `gh pr edit <N> --add-label <label>`, listed in pr_babysit.dispatch"
    if cmd not in ("pr", "issue") or sub not in ("comment", "edit", "review", "close", "reopen", "ready", "merge", "update-branch"):
        return None
    labels = [x for v in prguard._opts(more, ("--add-label",)) for x in v.split(",")]
    if not no_post and not labels:
        return None
    key = _target(real, more)
    if not key:
        return "refused: this call's PR could not be read, so pr_babysit.no_post_prs and dispatch cannot be checked"
    if key.lower() in no_post:
        return f"refused: {key} is in pr_babysit.no_post_prs: fix it by local commits and tell the user; nothing is posted"
    for label in labels:
        if not (cmd == "pr" and sub == "edit" and key in (owned(db) if db else {})):
            return f"refused: a label goes only on an open PR this project owns ({key})"
        if f"label:{label}" not in cfg_["dispatch"].get(key.split("#")[0].lower(), []):
            return f"refused: label {label!r} is not listed for {key.split('#')[0]} in pr_babysit.dispatch"
    return None


def _sets_labels(args: list[str], stdin_text: str | None) -> bool:
    """A `gh api` write that sets labels: a `labels` field name, a `labels` key in an --input body, or a GraphQL label
    mutation. Field values (a comment's words) are not read."""
    from . import prguard
    keys = [v.partition("=")[0].split("[")[0] for v in prguard._opts(args, ("-f", "-F", "--field", "--raw-field"))]
    body = prguard._api_text([x for v in prguard._opts(args, ("--input",)) for x in ("--input", v)], stdin_text)
    return "labels" in keys or bool(re.search(r"\"labels\"\s*:", body)) or any(
        re.search(r"labelIds|addLabelsToLabelable", v.partition("=")[2]) for v in prguard._opts(args, ("-f", "-F", "--field", "--raw-field"))
        if v.partition("=")[0] == "query")


def _ts(s) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def workflow_info(path: str, text: str) -> dict:
    """{"name", "file", "manual", "trigger", "labels"} of one workflow file, without a YAML parser: its `on:` block is the
    lines up to the next top-level key. `manual`: it runs only by dispatch or by a label."""
    name = re.search(r"^name:\s*(.+?)\s*(?:\s#.*)?$", text, re.M)
    block = re.search(r"^(?:on|'on'|\"on\"):(.*(?:\n(?:[ \t].*|#.*|\s*))*)", text, re.M)
    block = block.group(1) if block else ""
    dispatch = "workflow_dispatch" in block
    labels = []
    if "labeled" in block:
        labels = re.findall(r"github\.event\.label\.name\s*==\s*['\"]([^'\"]+)['\"]", text)
        labels += re.findall(r"contains\([^()]*labels[^()]*,\s*['\"]([^'\"]+)['\"]", text)
    trigger = " or ".join(["workflow_dispatch"] * dispatch + [f"label {x}" for x in dict.fromkeys(labels)]) or "none"
    manual = (labels and not re.search(r"opened|synchronize", block)) or (dispatch and not re.search(r"pull_request|push", block))
    return {"name": (name.group(1).strip("'\"") if name else path),
            "file": path.rsplit("/", 1)[-1], "manual": bool(manual), "trigger": trigger, "labels": list(dict.fromkeys(labels))}


def _flows(db, gh, repo: str, sha: str, cwd: str, now: float) -> list[dict] | None:
    """The repo's workflows at `sha`: the folder listing is kept by sha and each file's reading by its blob, so a
    call not answered yet (route: exit 75) is the only one repeated next sweep. None while any answer is missing."""
    cache = {k: v for k, v in (db.kv(FLOWS_KEY, {}) or {}).items() if now - float(v.get("at") or 0) < (86400 if "list" in v else 7 * 86400)}
    lk = f"{repo}@{sha}"
    listing = (cache.get(lk) or {}).get("list")
    if listing is None:
        listing = gh(["api", f"repos/{repo}/contents/.github/workflows?ref={sha}"], cwd)
        if not isinstance(listing, list):
            return None if listing is None else []
        listing = [{"name": f.get("name"), "path": f.get("path"), "sha": f.get("sha")} for f in listing]
        cache[lk] = {"at": now, "list": listing}
    flows, missing = [], False
    for f in [f for f in listing if str(f.get("name", "")).endswith((".yml", ".yaml"))]:
        k = f"{repo}:{f['path']}:{f.get('sha') or sha}"
        if k not in cache:
            one = gh(["api", f"repos/{repo}/contents/{f['path']}?ref={sha}"], cwd)
            if not isinstance(one, dict) or not one.get("content"):
                missing = True
                continue
            text = base64.b64decode(one["content"]).decode("utf-8", "replace")
            cache[k] = {"at": now, "info": workflow_info(f["path"], text)}
        flows.append(cache[k]["info"])
    db.set_kv(FLOWS_KEY, cache)
    return None if missing else flows


def _workflow(c: dict) -> str:
    flow = ((c.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {}
    return str(c.get("workflowName") or flow.get("name") or "")


def _not_run(daemon, pr: dict, key: str, gh, now: float, pending: bool) -> str:
    """The `pr_ci_not_run` text: workflows with only skipped checks, and (once nothing is pending) manual workflows
    `pr_babysit.dispatch` lists, by file, name or label, that never ran on the head."""
    sha, repo = pr.get("headRefOid"), key.split("#")[0]
    checks = pr.get("statusCheckRollup") or []
    listed = settings(getattr(daemon, "cfg", None))["dispatch"].get(repo, [])
    skip = any((c.get("conclusion") or "").upper() == "SKIPPED" for c in checks)
    flows = _flows(daemon.p.db, gh, repo, sha, str(daemon.p.root), now) if sha and (skip or listed) else None
    if flows is None:
        return ""
    by_name = {f["name"]: f"{f['file']}, trigger: {f['trigger']}" for f in flows}
    ran = {_workflow(c) for c in checks}
    ran_ok = {_workflow(c) for c in checks if (c.get("conclusion") or "").upper() != "SKIPPED"}
    skipped = {_workflow(c) or c.get("name") or "?" for c in checks if (c.get("conclusion") or "").upper() == "SKIPPED"}
    todo = {n: by_name.get(n, "trigger: none") for n in skipped - ran_ok}
    named = not checks or any(ran - {""})   # a read that names no workflow cannot say which never ran
    todo.update({f["name"]: f"{f['file']}, trigger: {f['trigger']}" for f in flows if f["manual"] and f["name"] not in ran and named and not pending
                 and (f["file"] in listed or f["name"] in listed or any(f"label:{x}" in listed for x in f.get("labels", [])))})
    return "; ".join(f"{n} ({t})" for n, t in sorted(todo.items()))


def _told(daemon, state: dict, key: str, url: str, sig: dict, ready: bool) -> None:
    """One chat line per status change since the last sweep, kept per PR and condition."""
    from . import chatdigest
    told = state.setdefault("told", {})
    now = {"state": sig["state"], "draft": sig["draft"], "decision": sig["decision"] or "",
           **({} if sig["mergeable"] == "UNKNOWN" else {"conflict": sig["mergeable"] == "CONFLICTING"})}
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
    from . import prguard
    cfg = settings(getattr(daemon, "cfg", None))
    url = pr.get("url") or t["pr_url"]
    key = prguard.pr_key(url) or url
    db, now = daemon.p.db, time.time()
    allstate = dict(db.kv(KEY, {}) or {})
    state = allstate.setdefault(key, {})
    _told(daemon, state, key, url, sig, pr.get("isDraft") is False)
    state["branch"], state["open"] = pr.get("headRefName") or "", sig["state"] == "OPEN"
    if pr.get("headRefOid"):
        state["head"] = pr["headRefOid"]   # overlap_check caches the changed files by it
    events = []
    if sig["state"] == "OPEN":
        text = _not_run(daemon, pr, key, gh, now, sig["checks"] == "pending")
        if text and state.get("notrun") != (mark := f"{pr.get('headRefOid')} {text}"):
            state["notrun"] = mark
            events.append(("pr_ci_not_run", f"PR {url}: checks skipped or never run on the head: {text}. Dispatch "
                           "or label what this change needs (the project's own PRs only)."))
        last = max([_ts(pr.get("updatedAt"))] + [_ts(c.get(k)) for c in pr.get("statusCheckRollup") or []
                                                 for k in ("completedAt", "startedAt")])
        hours = (now - last) / 3600 if last else 0
        who = [r.get("login") or r.get("name") for r in pr.get("reviewRequests") or []]
        quiet = key.lower() in {k.lower() for k in cfg["no_post_prs"]}
        ping = "" if quiet or not who else f" Ping them once (a comment ending with the AI tag): {PING!r}"
        if last and hours >= float(cfg["idle_h"]) and state.get("idle") != last:   # the ping is activity: once per quiet period
            state["idle"] = last
            events.append(("pr_idle", f"PR {url} idle for {hours:.0f} h (no commit, review, comment or check "
                           f"activity). Requested reviewers: {', '.join(x for x in who if x) or 'none'}."
                           f"{' No-post PR: tell the user, post nothing.' if quiet else ''}{ping}"))
    for kind, text in events:
        from . import screen as scr
        status = "muted" if scr.count_muted(db, "pr", text, "normal") else "queued"
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "pr", kind, "normal", text, status, t.get("id")))
    db.set_kv(KEY, allstate)


def route_check(daemon) -> None:
    """One `pr_route_silent` event while the route PRs are read through (delivery.gh_route) has not
    answered for over a day: the sweep is blind without saying so. Names the route, how long it has
    been silent and the user's own step to bring it back; fires once per silence and clears by itself
    once the route answers (its answer moves the silence window)."""
    from . import ghroute, screen as scr
    alias = ghroute.route(getattr(daemon, "cfg", None), str(daemon.p.root))
    st = {} if ghroute.local_ok() or not alias else ghroute.last_answer()
    db, now = daemon.p.db, time.time()
    if st.get("via") != alias or not st.get("at"):
        db.set_kv(ROUTE_KEY, {})   # our route has never answered: the gh-auth condition covers that
        return
    silent, told = now - float(st["at"]), {"via": alias, "at": float(st["at"])}
    if silent <= ROUTE_SILENT_S:
        if db.kv(ROUTE_KEY, {}):
            db.set_kv(ROUTE_KEY, {})   # the route answered: the condition clears
        return
    if db.kv(ROUTE_KEY, {}) == told:
        return   # this silence is already told
    db.set_kv(ROUTE_KEY, told)
    step = (f"run `ttp bridge gh-pull` on your workstation to drain the gh queue"
            if alias == ghroute.WORKSTATION else
            f"bring the machine behind ssh alias {alias!r} back, or fix delivery.gh_route")
    text = (f"PR watch is blind: the gh route {alias!r} PRs are read through has not answered for "
            f"{silent / 3600:.0f} h. {step}.")
    _post_event(daemon, "pr_route_silent", text, None)


def overlap_check(daemon, gh) -> None:
    """One `pr_overlap` event per pair of open PRs in the same repo that change the same files, one of
    them a PR this project owns: they must coordinate (one carries the change and the version bump,
    the other rebases). Each project's sweep records its open PRs and their changed files under the
    tt-project home, so every project's sweep sees every other's."""
    db = daemon.p.db
    own = {k: v for k, v in (db.kv(KEY, {}) or {}).items() if v.get("open")}
    files = {k: fs for k in own if (fs := _pr_files(db, gh, k, own[k].get("head"), str(daemon.p.root)))}
    _host_write(daemon, {k: {"head": own[k].get("head"), "files": fs} for k, fs in files.items()})
    known = _host_prs(time.time())
    had = set(db.kv(OVERLAP_KEY, {}) or {})
    pairs = []   # (a, b, shared files) of every overlapping pair this project has a say in
    keys = sorted(known)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if a not in own and b not in own:
                continue   # a pair between two other projects: their sweeps report it
            if a.split("#")[0] != b.split("#")[0]:
                continue
            shared = sorted(set(known[a]) & set(known[b]))
            if shared:
                pairs.append((a, b, shared))
    db.set_kv(OVERLAP_KEY, {f"{a}|{b}": 1 for a, b, _ in pairs})   # a pair that stops overlapping is told again when it returns
    for a, b, shared in pairs:
        if f"{a}|{b}" in had:
            continue   # once per pair while the overlap stands
        shown = ", ".join(shared[:SHOWN_FILES]) + (f" and {len(shared) - SHOWN_FILES} more" if len(shared) > SHOWN_FILES else "")
        _post_event(daemon, "pr_overlap",
               f"PRs {_pr_link(a)} and {_pr_link(b)} change the same files ({shown}): the projects behind "
               "them must coordinate - one carries the change and the version bump, the other rebases.", None)


def _post_event(daemon, kind: str, text: str, task) -> None:
    from . import screen as scr
    db = daemon.p.db
    status = "muted" if scr.count_muted(db, "pr", text, "normal") else "queued"
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
         (time.time(), "pr", kind, "normal", text, status, task))


def _pr_link(key: str) -> str:
    """`[owner/repo#N](url)` for a pr key."""
    return f"[{key}](https://github.com/{key.split('#')[0]}/pull/{key.split('#')[1]})"


def _pr_files(db, gh, key: str, head, cwd: str) -> list[str] | None:
    """The PR's changed file names, cached by its head: None while the read has no answer. A PR whose
    head is unknown is read but not cached, so a new head is never judged by the old files."""
    cache = db.kv(FILES_KEY, {}) or {}
    hit = cache.get(key)
    if head and hit and hit.get("head") == head and hit.get("files"):
        return hit["files"]
    repo, n = key.split("#")
    rows = gh(["api", f"repos/{repo}/pulls/{n}/files?per_page=100"], cwd)
    if not isinstance(rows, list):
        return None
    out = sorted({str(r.get("filename")) for r in rows if isinstance(r, dict) and r.get("filename")})
    if out and head:
        db.set_kv(FILES_KEY, {k: v for k, v in {**cache, key: {"head": head, "files": out}}.items()
                              if k == key or (db.kv(KEY, {}) or {}).get(k, {}).get("open")})
    return out or None


def _host_write(daemon, prs: dict) -> None:
    """Record this project's open PRs and their changed files for the host's other projects' sweeps."""
    from . import ghauth
    from .ghqueue import _project
    from .project import write_json

    def put():
        rec = _host_read()
        rec[_project(str(daemon.p.root)) or daemon.p.name] = {"at": time.time(), "prs": prs}
        for k, v in list(rec.items()):
            if time.time() - float(v.get("at") or 0) > HOST_STALE_S:
                del rec[k]
        write_json(ghauth._home(None) / HOST_FILE, rec, 0o600)
    try:
        ghauth._locked(None, put, block=True, lock="pr-overlap.lock")
    except OSError:
        pass


def _host_read() -> dict:
    from . import ghauth
    try:
        rec = json.loads((ghauth._home(None) / HOST_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _host_prs(now: float) -> dict[str, list[str]]:
    """{pr key: changed files} of every fresh host project's open PRs, ours included."""
    out = {}
    for v in _host_read().values():
        if now - float(v.get("at") or 0) > HOST_STALE_S:
            continue
        for k, pv in (v.get("prs") or {}).items():
            out[k] = list(pv.get("files") or [])
    return out