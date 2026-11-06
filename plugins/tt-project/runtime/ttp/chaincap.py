# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Continue chains: caps per lineage, and the next hop the daemon queues itself.

A lineage here is a chain of tasks linked back by `continues:<id>` and `follows:<id>` labels (the
latter on a task_add that `continues` a done task, whatever its title) and by `continue:` follow-ups (a task titled `continue: ...` whose parent is the task it continues). Its first task is
the root; each later one is a hop. Per lineage the runtime counts hops, the spend of all its tasks,
and stalls: the hops, counted back from the newest, that failed or made no progress.

Progress (a done hop, against the task before it in the chain; the root against the base ref):
- new commits: a commit made since the hop was created, on its own branch or a branch of an
  earlier task in the lineage, that the base does not contain; or
- new passing evidence: a `metrics` entry in its hand-off that is new or changed against the
  previous task's.
A code hop counts commits only (code_commits): ones whose committer time falls inside its runs, read
from its own worktree or clone (own_trees: the work tree its runs recorded, worktrees/t<id>, or one
named t<id> a few levels under the project root; also where the project root is no git repository),
its branch and pr_branch, and the shas its hand-off names. Metrics alone are never a code hop's
progress. A code hop with no new commit whose only proposal is a `continue:` gets no hop queued: one
NO_COMMIT_EVENT (NO_COMMIT_TRIGGER) names the task and its continue, and the coordinator decides.

Caps (`chains` in project.json; 0 turns one off): max_hops (DEFAULTS: 8), max_stalls (2 failed or
no-progress hops in a row) and max_usd (40 for the whole lineage). Once one is reached, or while
the lineage's area is under review.area_fail_cap (a reviewcap.REVIEW_AREA_EVENT within
reviewcap.AREA_WINDOW_S naming one of its tasks), a task_add that continues the lineage is refused,
and a CAP_EVENT makes the next coordinator turn a triggered one (TRIGGER). The only way past the
soft caps (max_stalls, the review area) is the action's `reason` naming what changed (REASON_RE: a
new approach, a rescoped spec, new evidence): given with the first task_add it passes in that same
action, recorded as an OVERRIDE_EVENT with the cap it lifted; the refusal names the field to add.
max_hops and max_usd (HARD) are spend safety: no reason lifts them.

Model-free hop (`chains.auto`, on by default): a done task whose only proposal is one `continue:`
follow-up, in a lineage below every cap, that made progress gets the follow-up queued by the daemon
with `continues` set (an AUTO_EVENT). A hop of a done task starts from the base, not its branch, with
one exception: a split. A code task whose branch has changes and that proposes a `continue:` hop, or
whose hand-off marks its head unfinished (marked_split), ended as a split (SPLIT_LABEL, split:<head>);
a `continue:` follow-up that runs only once the task has landed (post_landing: a deploy, live install or
check, or any follow-up that names `landed:#<the task>`) carries nothing forward, makes no split and
is never queued here; one that names a live step (live_step) reaches the coordinator NOT queued, with
deploy_proposal's words (one deploy task per target, start_when `landed:#<the task>`). Nor does one that
asks or measures, is of another kind or waits in its words on the task landing (code_hop): the task is
reviewed now and the coordinator decides on the follow-up. A split's
head is work in progress. It gets no review, and its hop takes the branch over (`continues:`, so
it starts from that head) and starts at once: a start_when `landed:` on a task of its own chain could
never pass, as those commits land only with the hop's own work (own_wait refuses it). The open tasks
that waited on the split task wait on the hop. The last hop, the first that ends without a split,
gets the chain's one review (split_hops). A hop that stalls (hop_stall: blocked, waiting on the user,
on a start_when or deferred past HOP_WAIT_S) carries nothing to review: the split head is reviewed now
(SPLIT_REVIEW_LABEL; a review moved to the hop waits on the split task again), and the last hop's review
covers only the commits after it (reviewed_split). The notice, and a local_only notice for a branch its review
still carries, go to the next digest (NOTICES_KEY) without waking the coordinator. Anything else
still goes to it.

Re-queue (requeue): the lone `continue:` hop of a done code task whose change no review approved and that
has not landed is no new task. The same task goes back to the queue, so it keeps its id, worktree, branch
and review chain: the follow-up's spec is appended as an `## Update`, its per-task budget is topped up to
a fresh one, its attempt count stays and its last hand-off (the prompt's previous attempt) stays. Each
re-queue counts as a hop under the caps (REQUEUED_LABEL), and its progress counts only the runs since.
An approved or landed task still gets a new hop.

The daemon appends branch_brief to the queued hop's spec, model-free: the parent branch's head and
its changed files with their hunk line ranges against the base ref, so the hop starts with less
orientation. It is bounded (BRIEF_MAX_LINES) and left out when the parent has no branch here.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import gitmemo, headline
from .db import DB, assigned_tier, continues_id, deferral, load_result

CONT_TITLE = re.compile(r"(?i)^\s*(?:continue|mechanical)\s*:")
# A hop that only runs checks, pushes or edits spec or PR text: queued at the light tier (hop_tier).
FIX_TITLE = re.compile(r"(?i)^\s*fix review #\d+")   # the daemon's review fix title, also used by hand
MECH_TITLE = re.compile(r"(?i)^\s*mechanical\s*:")
TIERS = ("light", "standard", "deep")
REASON_RE = re.compile(r"(?i)\b(?:new|different|changed|other)\s+(?:approach|evidence|design|strategy)\b"
                       r"|\bre-?scop\w*|\bnarrow\w*\s+(?:the\s+)?scope|\bscope[sd]?\s+(?:down|narrow\w*)")
MIN_REASON = 20
HARD = ("max_hops", "max_usd")   # caps no `reason` lifts (spend safety); max_stalls and review.area_fail_cap take one
REASON_HINT = ("add a `reason` field to the task_add that names what changed, in these words: new evidence, "
               f"a different approach or a narrower scope (at least {MIN_REASON} characters); it then passes in the same action")
DEFAULTS = {"max_hops": 8, "max_stalls": 2, "max_usd": 40.0, "auto": True, "requeue": True,
            "max_task_runs": 4, "max_idle_runs": 2}   # the last two: per task, at dispatch (runcap)
CAP_EVENT = "continue_cap"
OVERRIDE_EVENT = "continue_cap_override"
AUTO_EVENT = "continue_auto"
TRIGGER = "continue chain over its cap"
NO_COMMIT_EVENT = "continue_no_commit"   # a code hop with no new commit asked for its continue (no_commit_event)
NO_COMMIT_TRIGGER = "re-plan: no new commit"
HANDOFF_SHA = re.compile(r"(?<![\w-])[0-9a-f]{7,40}(?![\w-])")
MAX_NAMED = 10   # hand-off shas read per hop
TREE_DEPTH = 3     # levels under the project root own_trees looks for a hop's t<id> work tree
TREE_SCAN = 1000   # directories own_trees lists at most
FOLLOWS = "follows:"   # label of a task_add that `continues` a done task: a lineage link that takes nothing over
NOTICES_KEY = "chain_notices"   # kv: [{ts, text}]: hops the daemon queued, shown once in the next digest
MAX_CHAIN = 200
MAX_NOTICES = 30
BRIEF_MAX_LINES = 40   # branch_brief's bound, its header and '+N more files' line included
BRIEF_MAX_RANGES = 8   # hunk ranges listed per file before '...'
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
SPLIT_LABEL = "split:"   # a done code task that ended as a split: split:<its head>
SPLIT_REVIEW_LABEL = "split_review:"   # a review of a split head on its own, as its hop stalled: split_review:<head>
REQUEUED_LABEL = "requeued:"   # a code task re-queued for its own `continue:` hop (requeue): requeued:<unix time>
BUDGET_BASE_LABEL = "budget_base:"   # its per-task budget before its first re-queue: what each re-queue tops up to
HOP_WAIT_S = 3600   # a hop deferred longer than this stalls (hop_stall): its split head is reviewed without it
# A split hand-off's own words: a summary that opens "Split at/here/..." or names the split line, a head
# commit subject that says WIP.
SPLIT_SUMMARY = re.compile(r"(?i)^\W*split\b(?=\s*(?:at|here|mid|after|before|because|per|on|by|[(:;,.]|$))"
                           r"|\bpast the split line\b")
WIP_SUBJECT = re.compile(r"(?:^|[\s(\[])WIP(?=$|[\s):\]])|\(work in progress\)")
# A post-landing follow-up's own words (post_landing): 'after' or 'once' this task (it, this task or change,
# the change, #<id>, or 'the <a few words>': 'after the parser fix lands') lands, or 'after landed:#<id>',
# and a live step in its title or that sentence; a title that lands, reviews, pushes, fixes or finishes the
# change still carries it forward.
AFTER_LANDING = re.compile(r"(?i)\b(?:after|once)\s+(?:(?:it|this\s+(?:task|change)|the\s+change|#(\d+))\s+"
                           r"(?:lands|has\s+landed|is\s+landed|is\s+pushed|reaches\s+the\s+push\s+branch)\b"
                           r"|landed:\s*#?(\d+)(?!\d)"
                           r"|the\s+(?:[a-z][\w.-]*\s+){1,4}(?:lands|has\s+landed|is\s+landed)\b)")
SENTENCE_END = re.compile(r"[.!?;](?=\s|$)|\n")
# The live steps a post-landing follow-up names (deploy, install, upgrade, rollout or roll out, restart, a live
# step, check or run, verify on a host): one list for post_landing's wording and for live_step.
LIVE_STEP = re.compile(r"(?i)\b(?:deploy\w*|(?:re-?)?install\w*|upgrad\w*|roll(?:s|ed|ing)?(?:\s+(?:it|them))?[\s-]?outs?"
                       r"|restart\w*|live\s+steps?"
                       r"|live\s+(?:check|test|run)\w*|verif\w*\b[^.;\n]{0,40}?\bon\s+(?:a|the|each|every|its)?\s*"
                       r"(?:host|box|machine|node)s?)\b(?!\.\w)")   # not a file name (install.sh)
CARRIES = re.compile(r"(?i)^\s*(?:continue|mechanical)\s*:\s*(?:then\s+|and\s+)?(?:re-?)?(?:land|review|push|fix"
                     r"|finish|complete)\w*\b|\b(?:land|review|push|fix|finish)(?:e?s)?\s+(?:it|this|the|its)\b")
WAIT_KEYS = ("start_when", "retry_when", "wait", "waiting_for")


def caps(cfg: dict) -> dict:
    c = cfg.get("chains") or {}
    out: dict = {k: c.get(k, True) not in (False, 0, "off", "false") for k in ("auto", "requeue")}
    for k in ("max_hops", "max_stalls", "max_usd"):
        try:
            out[k] = max(0.0, float(c.get(k, DEFAULTS[k])))
        except (TypeError, ValueError):
            out[k] = float(DEFAULTS[k])
    return out


def follows_id(task: dict) -> int | None:
    """The done task a follow-up was added to continue (its FOLLOWS label), whatever its title."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except ValueError:
        return None
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith(FOLLOWS) and lb[len(FOLLOWS):].isdigit():
            return int(lb[len(FOLLOWS):])
    return None


def prev_of(task: dict) -> int | None:
    """The task `task` continues: its FOLLOWS or `continues:` label, or the parent of a `continue:` follow-up.
    FOLLOWS first: a follow-up that publishes another code task's branch also continues that task
    (publishkind), but its lineage is the task it follows."""
    if (c := follows_id(task)) is not None or (c := continues_id(task)) is not None:
        return c
    if task.get("parent") and CONT_TITLE.match(task.get("title") or ""):
        return int(task["parent"])
    return None


def is_review_fix(task: dict) -> bool:
    """Whether hop `task` is a review fix (the daemon's review fix path labels it `review_fix:<review>`;
    a fix the coordinator added by title is matched by FIX_TITLE) rather than a continuation. A `continue:`
    or FOLLOWS hop that took a failed review's findings over stays a continuation: the split made it."""
    title = task.get("title") or ""
    if CONT_TITLE.match(title) or follows_id(task) is not None:
        return False
    try:
        labels = json.loads(task.get("labels") or "[]")
    except ValueError:
        labels = []
    return any(isinstance(lb, str) and lb.startswith("review_fix:") for lb in (labels if isinstance(labels, list) else [])) \
        or bool(FIX_TITLE.match(title))


def requeues(task: dict | None) -> list[int]:
    """The times code task `task` was re-queued for its own `continue:` hop (REQUEUED_LABEL), oldest first."""
    try:
        labels = json.loads((task or {}).get("labels") or "[]")
    except ValueError:
        return []
    return sorted(int(lb[len(REQUEUED_LABEL):]) for lb in labels if isinstance(lb, str)
                  and lb.startswith(REQUEUED_LABEL) and lb[len(REQUEUED_LABEL):].isdigit()) \
        if isinstance(labels, list) else []


def hop_count(ch: list[dict]) -> int:
    """The hops of chain `ch`: its tasks after the root, and each re-queue of one of them."""
    return max(len(ch) - 1, 0) + sum(len(requeues(t)) for t in ch)


def chain(db: DB, task_id: int) -> list[dict]:
    """The lineage ending at `task_id`, root first. Reviews are not hops: a review starts none."""
    out: list[dict] = []
    seen: set[int] = set()
    t = db.task(task_id)
    while t and t["id"] not in seen and len(out) < MAX_CHAIN and t["kind"] != "review":
        out.append(t)
        seen.add(t["id"])
        i = prev_of(t)
        t = db.task(i) if i is not None else None
    return out[::-1]


def _metrics(task: dict | None) -> dict:
    m = load_result((task or {}).get("result")).get("metrics")
    return m if isinstance(m, dict) else {}


def run_windows(db: DB, task: dict, now: float | None = None) -> list[tuple[int, int]]:
    """The whole seconds `task`'s runs spanned (a running one up to now); with no run recorded, from its
    creation to now. A re-queued task's (requeue) only since its last re-queue."""
    now = time.time() if now is None else now
    since = max(requeues(task), default=0)
    rows = db.q("SELECT started, ended FROM runs WHERE task=? AND started IS NOT NULL AND started>=?",
                (task["id"], since))
    spans = [(float(r["started"]), float(r["ended"] or now)) for r in rows] \
        or [(float(since or task.get("created") or 0), now)]
    return [(int(a), -int(-b // 1)) for a, b in spans]


def named_shas(task: dict) -> list[str]:
    """The commit hashes `task`'s hand-off names: `commit`, `head`, `sha`, a `commits` list, then any in
    its summary (unresolved, at most MAX_NAMED)."""
    res = load_result(task.get("result"))
    named = [str(res.get(k) or "").strip() for k in ("commit", "head", "sha")]
    commits = res.get("commits")
    named += [str(c).strip() for c in commits] if isinstance(commits, list) else []
    named += HANDOFF_SHA.findall(str(res.get("summary") or ""))
    return [c for c in dict.fromkeys(named) if HANDOFF_SHA.fullmatch(c)][:MAX_NAMED]


def own_trees(p, hop: dict) -> list[Path]:
    """The git work trees code hop `hop` worked in, never the project root: the top level of the
    working directory its runs recorded (run.json cwd) under the project root, and worktrees/t<id> or
    a directory named t<id> at most TREE_DEPTH levels under the project root (a worktree outside the
    default folder) that is its own top level. The search skips hidden and symlinked directories and
    the harness folder, and lists at most TREE_SCAN directories."""
    from . import worktree
    root, name = Path(p.root).resolve(), f"t{hop['id']}"
    cwds, named = [], [Path(p.worktrees) / name]
    for r in p.db.q("SELECT dir FROM runs WHERE task=? AND dir IS NOT NULL", (hop["id"],)):
        try:
            cwd = json.loads((Path(r["dir"]) / "run.json").read_text()).get("cwd")
        except (OSError, ValueError, AttributeError):
            continue
        cwds += [Path(cwd)] if isinstance(cwd, str) and cwd else []
    skip = {Path(b).resolve() for b in (getattr(p, "base", None), p.worktrees) if b}
    level, listed = [root], 0
    for _ in range(TREE_DEPTH):
        nxt: list[Path] = []
        for d in level:
            if listed >= TREE_SCAN:
                break
            listed += 1
            try:
                with os.scandir(d) as it:
                    subs = [Path(e.path) for e in it if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            named += [s for s in subs if s.name == name]
            nxt += [s for s in subs if s.name != name and s not in skip]
        level = nxt

    def top(d: Path) -> Path | None:
        try:
            if d.is_dir() and worktree.is_git(d):
                return Path(worktree._git(d, "rev-parse", "--show-toplevel", check=False)).resolve()
        except Exception:
            pass
        return None
    trees = [t for d in cwds if (t := top(d)) and t != root and t.is_relative_to(root)]
    trees += [t for d in named if (t := top(d)) and t == d.resolve()]
    return list(dict.fromkeys(trees))


def code_commits(p, hop: dict, earlier=(), now: float | None = None, wins=None) -> int:
    """The commits code hop `hop` made: committer time inside its runs (run_windows, or `wins`), read from its own
    worktree or clone (own_trees), its branch and pr_branch (each where it resolves: the worktree, the
    project root) less the base (in a standalone clone, its origin/HEAD) and the `earlier` tasks' branches
    it started from, and the shas its hand-off names. A source git cannot read adds none."""
    from . import worktree
    wins = wins or run_windows(p.db, hop, now)
    lo = min(a for a, _ in wins)
    found: dict[str, int] = {}

    def log(repo, *args: str) -> None:   # missing revs add nothing (--ignore-missing)
        try:
            out = worktree._git(repo, "log", "--ignore-missing", "--format=%H %ct", *args, check=False)
        except Exception:
            return
        for line in out.splitlines():
            sha, _, ct = line.partition(" ")
            if ct.isdigit():
                found[sha] = int(ct)

    own = own_trees(p, hop)
    pr = worktree.recorded_pr_branch(hop) or ""
    branches = [b for b in dict.fromkeys((hop.get("branch") or "", pr)) if b and worktree.valid_branch(b)]
    shas = named_shas(hop)
    repos = [*own, *([p.root] if p.root.is_dir() and worktree.is_git(p.root) else [])]
    common = lambda r: worktree._git(r, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)  # noqa: E731
    try:   # the base only in the root's own refs (a linked worktree shares them; a clone's same name is its own)
        base = f"^{worktree.resolve_base(p)}" if p.root in repos else ""
    except Exception:
        base = ""
    not_ = [f"^refs/heads/{b}" for b in dict.fromkeys(earlier) if b and worktree.valid_branch(b)]
    for repo in repos:
        same = repo == p.root or (p.root in repos and common(repo) == common(p.root))
        # a standalone clone: less its upstream default branch (commits pulled in are not the hop's own);
        # not every remote ref, as the hop's own pushed branch still counts
        less = [base] if base and same else [] if same else ["^refs/remotes/origin/HEAD"]
        if refs := (["HEAD"] if repo in own else []) + [f"refs/heads/{b}" for b in branches]:
            log(repo, f"--since=@{lo}", "--max-count=500", *refs, *not_, *less)
        if shas:
            log(repo, "--no-walk=unsorted", *shas)
    return sum(1 for ct in found.values() if any(a <= ct <= b for a, b in wins))


def progress(p, ch: list[dict], i: int) -> str:
    """Why hop `ch[i]` made progress (see the module's rule), or '' when it did not."""
    from . import worktree
    hop = ch[i]
    if hop["status"] != "done":
        return ""
    if hop.get("kind") == "code":   # commits only: metrics alone are no code hop's progress
        try:
            earlier = tuple(t.get("branch") or "" for t in ch[:i])
            n = gitmemo.cached(("chaincap.code_commits", hop["id"], hop.get("branch") or "", earlier,
                                tuple(named_shas(hop))), lambda: code_commits(p, hop, earlier))
        except Exception:
            n = 0
        return f"{n} new commit{'' if n == 1 else 's'}" if n else ""
    def count() -> str:
        base = worktree.resolve_base(p)
        refs = [b for t in ch[:i + 1] if (b := t.get("branch"))
                and worktree._git(p.root, "rev-parse", "--verify", "--quiet", f"{b}^{{commit}}", check=False)]
        return worktree._git(p.root, "rev-list", "--count", f"--since=@{int(hop['created'] or 0)}", *refs,
                             f"^{base}", check=False) if refs else ""
    try:   # read once within a gitmemo scope (the daemon warms it before the finish transaction)
        n = gitmemo.cached(("chaincap.progress", tuple(t.get("branch") or "" for t in ch[:i + 1]),
                            int(hop["created"] or 0)), count)
        if n.isdigit() and int(n):
            return f"{n} new commit{'' if n == '1' else 's'}"
    except Exception:   # unreadable branches: only the evidence can show progress
        pass
    old = _metrics(ch[i - 1]) if i else {}
    new = sorted(str(k) for k, v in _metrics(hop).items() if k not in old or old[k] != v)
    return "new evidence: " + ", ".join(new[:5]) if new else ""


def stalls(p, ch: list[dict]) -> int:
    """Hops in a row, back from the newest, that failed or made no progress (the root is no hop)."""
    n = 0
    for i in range(len(ch) - 1, 0, -1):
        if progress(p, ch, i):
            break
        n += 1
    return n


def spend(ch: list[dict]) -> float:
    return sum(float(t.get("spent_usd") or 0) for t in ch)


def area_held(db: DB, ch: list[dict], now: float | None = None) -> int | None:
    """The root of a review area under review.area_fail_cap that holds one of the chain's tasks."""
    from . import reviewcap
    now = time.time() if now is None else now
    ids = {t["id"] for t in ch}
    for r in db.q("SELECT data FROM events WHERE kind=? AND ts>? ORDER BY ts DESC",
                  (reviewcap.REVIEW_AREA_EVENT, now - reviewcap.AREA_WINDOW_S)):
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        if isinstance(d, dict) and ids & {int(x) for x in d.get("tasks") or [] if str(x).isdigit()}:
            return int(d.get("root") or 0)
    return None


def cap_hit(p, cfg: dict, ch: list[dict]) -> tuple[str, str]:
    """The cap another hop after the chain `ch` reaches and why: (its key, the reason), ('', '') when it may follow."""
    if not ch:
        return "", ""
    c, root = caps(cfg), ch[0]
    hops, usd = hop_count(ch), spend(ch)
    head = f"the continue chain of #{root['id']} ({str(root['title'])[:80]}) "
    if c["max_hops"] and hops + 1 > c["max_hops"]:
        return "max_hops", head + f"has {hops} hops; chains.max_hops is {c['max_hops']:g}"
    if c["max_usd"] and usd >= c["max_usd"]:
        return "max_usd", head + f"spent ${usd:.2f}; chains.max_usd is ${c['max_usd']:g}"
    if (a := area_held(p.db, ch)) is not None:
        return "area_fail_cap", head + f"is in the review area of #{a}, under review.area_fail_cap (re-plan it first)"
    if c["max_stalls"] and (n := stalls(p, ch)) >= c["max_stalls"]:
        return "max_stalls", head + (f"ended with {n} failed or no-progress hops in a row; chains.max_stalls is "
                                     f"{c['max_stalls']:g}")
    return "", ""


def refusal(p, cfg: dict, ch: list[dict]) -> str:
    """Why another hop after the chain `ch` is refused, '' when it may follow."""
    return cap_hit(p, cfg, ch)[1]


def reason_ok(reason) -> bool:
    """A reason that names what changed: a new approach, a rescoped spec or new evidence."""
    r = str(reason or "").strip()
    return len(r) >= MIN_REASON and bool(REASON_RE.search(r))


def check_add(p, cfg: dict, prev: int, reason) -> str:
    """For a task_add continuing `prev`: '' when its lineage is under every cap, a note when `reason`
    takes it past a soft one (max_stalls, review.area_fail_cap) in the same action; otherwise raises
    ValueError naming the field to add, with a CAP_EVENT for the next turn (once per lineage and hop
    count). No reason lifts a HARD cap (max_hops, max_usd)."""
    db = p.db
    ch = chain(db, prev)
    cap, why = cap_hit(p, cfg, ch)
    if not why:
        return ""
    hard = cap in HARD
    data = {"root": ch[0]["id"], "tip": prev, "hops": hop_count(ch), "usd": round(spend(ch), 2), "why": why, "cap": cap}
    if not hard and reason_ok(reason):
        db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (time.time(), "coordinator", OVERRIDE_EVENT, "low", f"{why}; passed with reason: {str(reason)[:300]}",
              json.dumps({**data, "reason": str(reason)[:300]}), "handled", prev))
        return f"task_add continues #{prev} past its cap ({why}) with reason: {str(reason)[:200]}"
    fp = f"{CAP_EVENT}:{ch[0]['id']}:{hop_count(ch)}"
    way = (f"chains.{cap} is hard: no `reason` lifts it" if hard else
           "a task_add that continues it passes only with a `reason` naming what changed")
    if not db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
             (time.time(), "coordinator", CAP_EVENT, fp, "normal",
              f"{TRIGGER}: {why}. Another hop is refused. Re-plan: narrow the scope, change the approach, or stop "
              f"and report what is left; {way}.", json.dumps(data), "queued", prev))
    if hard:
        hint = f"chains.{cap} is hard and no `reason` lifts it: re-plan (narrow the scope) or stop and report what is left"
    elif str(reason or "").strip():
        hint = f"its `reason` does not name what changed; {REASON_HINT}"
    else:
        hint = f"it has no `reason`: {REASON_HINT}"
    raise ValueError(f"task_add continues #{prev} refused: {why}; {hint}")


def hop_ready(p, cfg: dict, task: dict, fup: dict) -> str:
    """Why `fup`, the only (`continue:`) follow-up of done `task`, may be queued by the daemon (`task`
    made progress and its lineage is under every cap), '' when it may not."""
    if not caps(cfg)["auto"] or not CONT_TITLE.match(str(fup.get("title") or "")):
        return ""
    ch = chain(p.db, task["id"])
    if not ch or refusal(p, cfg, ch):
        return ""
    return progress(p, ch, len(ch) - 1)


def no_commit_event(p, cfg: dict, task: dict, fup: dict) -> bool:
    """For done code task `task` whose only proposal is the `continue:` follow-up `fup` and that hop_ready
    refused for no new commit alone (auto on, its lineage under every cap): one NO_COMMIT_EVENT naming
    the task and that continue, so the coordinator decides. Whether one was raised now."""
    if task.get("kind") != "code" or not caps(cfg)["auto"] or not CONT_TITLE.match(str(fup.get("title") or "")):
        return False
    ch = chain(p.db, task["id"])
    if not ch or refusal(p, cfg, ch) or progress(p, ch, len(ch) - 1):
        return False
    fp = f"{NO_COMMIT_EVENT}:{task['id']}"
    if p.db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
        return False
    title, spec = str(fup.get("title") or "").strip()[:200], str(fup.get("spec") or "").strip()[:500]
    text = (f"{NO_COMMIT_TRIGGER}: code task #{task['id']} ({str(task['title'])[:80]}) ended with no new commit "
            f"in its runs, so its continue was not queued: {title!r}. Queue it with continues #{task['id']} "
            f"if work is left, re-plan it, or drop it.")
    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
           (time.time(), f"task:{task['id']}", NO_COMMIT_EVENT, fp, "normal", text,
            json.dumps({"root": ch[0]["id"], "task": task["id"], "hops": len(ch) - 1, "continue": title, "spec": spec}),
            "queued", task["id"]))
    return True


def marked_split(result: dict, subject: str = "") -> bool:
    """Whether a code hand-off marks its head unfinished: `"split": true` or `"wip": true`, a summary in a
    split's words (SPLIT_SUMMARY), or a head commit whose subject says WIP."""
    result = result if isinstance(result, dict) else {}
    return result.get("split") is True or result.get("wip") is True \
        or bool(SPLIT_SUMMARY.search(str(result.get("summary") or ""))) or bool(WIP_SUBJECT.search(subject or ""))


def waits_landing(probe, task_id: int) -> bool:
    """Whether a start_when, retry_when or wait names task `task_id`'s landing: `landed:#<id>` or a
    `ttp landed --task <id>` / `ttp landed #<id>` term."""
    return bool(re.search(rf"(?i)\blanded:\s*#?{int(task_id)}(?!\d)|\bttp\s+landed\s+(?:--task\s+)?#?{int(task_id)}"
                          rf"(?![\d\w])", str(probe or "")))


def post_landing(fup: dict, task_id: int) -> bool:
    """Whether a follow-up of task `task_id` runs only after that task lands, so it is no hop of a split:
    its start_when, wait, title or spec names the landing (waits_landing: `landed:#<task_id>` or `ttp
    landed --task <task_id>`, decisive: such a task can never carry the work to its landing), or its title and spec say it runs after or once the task lands (AFTER_LANDING,
    no other task id in that phrase) and name a live step (LIVE_STEP), with no title that lands,
    reviews, pushes, fixes or finishes the change (CARRIES). The live step counts only in the title or in
    the sentence that says so, never anywhere in the spec."""
    fup = fup if isinstance(fup, dict) else {}
    title = str(fup.get("title") or "")
    text = f"{title}\n{fup.get('spec') or ''}"
    if any(waits_landing(v, task_id) for v in (*(fup.get(k) for k in WAIT_KEYS), text)):
        return True
    if CARRIES.search(title):
        return False
    for m in AFTER_LANDING.finditer(text):
        if (ref := m.group(1) or m.group(2)) and ref != str(task_id):
            continue
        start = max((e.end() for e in SENTENCE_END.finditer(text, 0, m.start())), default=0)
        end = SENTENCE_END.search(text, m.end())
        if LIVE_STEP.search(title) or LIVE_STEP.search(text[start:end.start() if end else len(text)]):
            return True
    return False


# A `continue:` follow-up that asks or measures rather than carrying the change (code_hop): its title's first
# words after the prefix.
QUESTION_TITLE = re.compile(r"(?i)^\s*(?:continue|mechanical)\s*:\s*(?:then\s+)?(?:measure\w*|ask\w*|question\w*"
                            r"|find\s+out|(?:check|confirm|see)\s+(?:whether|if)|how\s+(?:much|many|often)"
                            r"|does|did|is|are|why)\b")
# 'after' or 'once' this task lands (AFTER_LANDING's own-task forms, any step).
OWN_LANDING = re.compile(r"(?i)\b(?:after|once|when)\s+(?:it|this\s+(?:task|change)|the\s+change|#(\d+))\s+"
                         r"(?:lands|has\s+landed|is\s+landed|is\s+pushed|reaches\s+the\s+push\s+branch)\b")


# A `continue:` follow-up that only waits: on checks, CI, a detached job or run, a push or a landing (waits_only).
WAIT_TITLE = re.compile(r"(?i)^\s*(?:continue|mechanical)\s*:\s*(?:then\s+)?(?:(?:wait|await|poll|watch)\b"
                        r"|(?:report|record|collect|read|check|confirm)\s+(?:on\s+)?(?:the\s+|its\s+|my\s+)?"
                        r"(?:[\w-]+\s+)?(?:checks?|ci|push(?:es)?|landing|job|run|results?|outcome)\b)")
# Words of a step that changes something: such a follow-up does more than wait.
WORK_STEP = re.compile(r"(?i)\b(?:fix|implement|add|write|edit|change|refactor|rebase|resolve|rename|remove|"
                       r"delete|port|build|split|trim)\w*")
WAITS_ONLY_HINT = "hand off waiting with retry_when and on_pass_cmd"


def waits_only(fup: dict) -> bool:
    """Whether `continue:` follow-up `fup` only waits (WAIT_TITLE) with no step that changes something in its
    title or spec (WORK_STEP): a waiting hand-off (retry_when, on_pass_cmd) does that without a new run."""
    fup = fup if isinstance(fup, dict) else {}
    title = str(fup.get("title") or "")
    return bool(WAIT_TITLE.match(title)) and not WORK_STEP.search(f"{title}\n{fup.get('spec') or ''}")


def code_hop(fup: dict, task_id: int) -> bool:
    """Whether a `continue:` follow-up of code task `task_id` carries its change on its branch, so the task
    is a split with no review of its own: no note, no post-landing step (post_landing), no other kind than
    code, no question or measurement (QUESTION_TITLE), and no wait in its words on this task landing
    (OWN_LANDING), unless its title lands, pushes or fixes the change (CARRIES). Any other one could never
    bring the review: the task is reviewed now."""
    fup = fup if isinstance(fup, dict) else {}
    title = str(fup.get("title") or "")
    if not CONT_TITLE.match(title) or post_landing(fup, task_id) \
            or str(fup.get("kind") or "code").strip().lower() != "code":
        return False
    if CARRIES.search(title):
        return True
    if QUESTION_TITLE.match(title):
        return False
    return not any((m.group(1) or str(task_id)) == str(task_id)
                   for m in OWN_LANDING.finditer(f"{title}\n{fup.get('spec') or ''}"))


def live_step(fup: dict) -> bool:
    """Whether a follow-up's title or spec names a live step (LIVE_STEP)."""
    fup = fup if isinstance(fup, dict) else {}
    return bool(LIVE_STEP.search(f"{fup.get('title') or ''}\n{fup.get('spec') or ''}"))


def deploy_proposal(fup: dict, task_id: int) -> str:
    """The coordinator's words for a `continue:` follow-up of task `task_id` that runs only after it lands
    (post_landing) and names a live step: no hop of it (it would wait on its own chain's landing, or deploy
    beside the task that already does), but one deploy task per target waiting on the landing; '' otherwise."""
    if not post_landing(fup, task_id) or not live_step(fup):
        return ""
    return (f"it runs only after #{task_id} lands and names a live step, so no hop of #{task_id} was queued: add "
            f"one deploy task per target with start_when landed:#{task_id} (no continues: a landed wait inside "
            f"its own chain is refused), unless an open task already deploys it, or drop it")


def split_head(task: dict | None) -> str:
    """The head a done code task left unfinished (its SPLIT_LABEL), or ''."""
    try:
        labels = json.loads((task or {}).get("labels") or "[]")
    except ValueError:
        return ""
    return next((lb[len(SPLIT_LABEL):] for lb in labels if isinstance(lb, str) and lb.startswith(SPLIT_LABEL)), "") \
        if isinstance(labels, list) else ""


def split_hops(db: DB, task: dict) -> list[dict]:
    """The split tasks `task` finishes, root first: each task before it in its chain, back to the first
    that did not end as a split."""
    ch = chain(db, task["id"])[:-1]
    out: list[dict] = []
    for t in reversed(ch):
        if not split_head(t):
            break
        out.append(t)
    return out[::-1]


def hop_stall(task: dict | None, now: float | None = None) -> str:
    """Why open hop `task` (a task row) holds its split head's review back no longer, or '' while it runs or
    starts soon: it is blocked, its waiting hand-off waits on the user's step (unblock.classify_wait), it
    waits on a start_when probe (a hop otherwise starts at once), or it is deferred past HOP_WAIT_S."""
    if not task or task.get("status") not in ("queued", "blocked"):
        return ""
    if task["status"] == "blocked":
        return "is blocked (" + headline.clip(str(task.get("blocked_reason") or "no reason given"), 160) + ")"
    from . import unblock
    result = load_result(task.get("result"))
    if result.get("status") == "waiting" and unblock.classify_wait(result)[0] == "user":
        return "waits on the user (" + headline.clip(str(result.get("waiting_for") or result.get("retry_when")
                                                         or "a step only the user can take"), 160) + ")"
    d = deferral(task)
    if d.get("when"):
        return "waits on its start_when (" + headline.clip(str(d.get("why") or d["when"]), 160) + ")"
    now = time.time() if now is None else now
    later = max(float(task.get("not_before") or 0), float(d.get("after") or 0))
    return f"is deferred {(later - now) / 3600:.1f} h" if later > now + HOP_WAIT_S else ""


def stalled_hop(db: DB, task_id: int, now: float | None = None) -> tuple[dict, str] | None:
    """The open hop of done split task `task_id` that stalls (hop_stall), with why; None when none does."""
    for t in db.q("SELECT * FROM tasks WHERE labels LIKE ? AND status IN ('queued','blocked') ORDER BY id",
                  (f'%"continues:{task_id}"%',)):
        if continues_id(t) == task_id and (why := hop_stall(t, now)):
            return t, why
    return None


def reviewed_split(db: DB, hops: list[dict]) -> tuple[int, dict] | None:
    """(index in `hops`, review) of the last split hop whose head a review of its own (SPLIT_REVIEW_LABEL)
    took up, running or passed; None when none did: the chain's last hop then reviews the whole chain."""
    for i in range(len(hops) - 1, -1, -1):
        head = split_head(hops[i])
        r = db.one("SELECT * FROM tasks WHERE kind='review' AND status IN ('running','done') AND labels LIKE ? "
                   "ORDER BY id DESC", (f'%"{SPLIT_REVIEW_LABEL}{head}"%',)) if head else None
        if r:
            return i, r
    return None


def own_wait(p, lineage: list[int], probe: str | None) -> str:
    """Why a start_when `probe` on a task that continues the tasks of `lineage` (directly or through
    its chain) can never pass, or '': a `landed:` wait on one of them whose work has not landed, as
    that work lands only with this task's own."""
    from . import landed
    for ref in landed.task_refs(probe):
        n = int(ref) if ref.isdigit() else 0
        if n in lineage and not p.db.one("SELECT 1 FROM task_superseded WHERE task=? UNION ALL "
                                         "SELECT 1 FROM task_landings WHERE task=?", (n, n)):
            return (f"start_when {str(probe).strip()[:160]!r} waits on #{n}, which this task continues (directly or "
                    f"through its continue chain), and #{n}'s work has not landed: it lands only with this task's "
                    f"own work, so the wait could never pass. Drop the start_when (a continue hop starts at once "
                    f"from its chain's branch), or wait on a task outside its chain")
    return ""


def hop_tier(task: dict, fup: dict) -> str:
    """The tier of the hop queued for `fup`: the parent's queued tier (db.assigned_tier, never one the
    daemon raised for a failed try's retry), or lower when the follow-up asks for it with `"tier"` or a
    `mechanical:` title (light). Never above it: a hop that needs more is retried a tier up by the daemon."""
    tier = assigned_tier(task)
    tier = tier if tier in TIERS else "standard"
    want = "light" if MECH_TITLE.match(str(fup.get("title") or "")) else str(fup.get("tier") or "").strip().lower()
    return want if want in TIERS and TIERS.index(want) < TIERS.index(tier) else tier


def branch_brief(p, task: dict) -> str:
    """The block the daemon appends to a hop's spec: `task`'s branch head and its changed files, each
    with its hunk line ranges (on the head; `-N`: lines deleted after line N) against the merge base with
    the base ref, at most BRIEF_MAX_LINES lines. '' when the task has no branch, the branch is missing
    or git cannot read it: the hop is queued without it."""
    from . import landed
    branch = str(task.get("branch") or "").strip()
    if not branch:
        return ""
    try:   # within a gitmemo scope read once per branch and head (warmed before the finish transaction)
        head = gitmemo.cached(("chaincap.brief_head", branch), lambda: landed._commit(p.root, f"refs/heads/{branch}"))
        if not head:
            return ""
        return gitmemo.cached(("chaincap.branch_brief", branch, head), lambda: _brief(p, branch, head))
    except Exception:
        return ""


def _brief(p, branch: str, head: str) -> str:
    from . import landed, worktree
    try:
        repo = p.root
        out = [f"Parent branch {branch}, head {head} (added by the daemon)."]
        ref = worktree.base_ref(p)
        base = landed._git(repo, "merge-base", ref, head).stdout.strip() if ref else ""
        if not base:
            return out[0]
        git = ("-c", "core.quotePath=false", "diff", "--no-renames", "--no-color", "--no-ext-diff")
        stat = landed._git(repo, *git, "--numstat", base, head)
        hunks = landed._git(repo, *git, "--unified=0", base, head)
        if stat.returncode or hunks.returncode:
            return out[0]
        ranges: dict[str, list[str]] = {}
        cur, header = "", False   # file names only from a file's header, never from its changed lines
        for ln in hunks.stdout.splitlines():
            if ln.startswith("diff --git "):
                cur, header = "", True
            elif header and ln.startswith(("--- a/", "+++ b/")):
                cur = ln[6:]
            elif (m := HUNK.match(ln)) and cur:
                header = False
                start, n = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                ranges.setdefault(cur, []).append("deleted" if n == start == 0 else f"-{start}" if n == 0 else
                                                  str(start) if n == 1 else f"{start}-{start + n - 1}")
        files = []
        for ln in stat.stdout.splitlines():
            parts = ln.split("\t", 2)
            if len(parts) == 3:
                files.append(parts)
        if not files:
            return out[0]
        out.append(f"Changed against {ref} (merge base {base[:12]}), {len(files)} "
                   f"file{'s' if len(files) > 1 else ''}, line ranges on the head:")
        room = BRIEF_MAX_LINES - len(out)
        shown = files if len(files) <= room else files[:room - 1]
        for add, rm, name in shown:
            rs = ranges.get(name, [])
            where = ", ".join(rs[:BRIEF_MAX_RANGES]) + (", ..." if len(rs) > BRIEF_MAX_RANGES else "")
            size = "binary" if add == "-" else f"+{add} -{rm}"
            out.append(f"- {name} ({size})" + (f": {where}" if where else ""))
        if len(shown) < len(files):
            out.append(f"+{len(files) - len(shown)} more files")
        return "\n".join(out)
    except Exception:
        return ""


def hop_spec(p, task: dict, fup: dict) -> str:
    """The spec auto_hop queues `fup` with: its own, then `task`'s branch_brief."""
    spec = str(fup.get("spec") or "")
    if brief := branch_brief(p, task):
        spec = f"{spec.rstrip()}\n\n{brief}" if spec.strip() else brief
    return spec


def auto_hop(p, cfg: dict, task: dict, fup: dict, why: str | None = None,
             refused: list | None = None) -> tuple[int, str] | None:
    """Queue `fup` when hop_ready (`why`, when the caller checked it already): (new task id, why it
    progressed), else None (task_add's refusals then go into `refused`). The hop of a split task (split_head) takes its branch over and starts at
    once; it never waits on that task's landing, which comes only with the hop's own work."""
    from . import coordinator as coord, publishkind
    db = p.db
    why = hop_ready(p, cfg, task, fup) if why is None else why
    if not why:
        return None
    ch = chain(db, task["id"])
    title = str(fup["title"]).strip()[:200]
    spec = hop_spec(p, task, fup)
    action = {"type": "task_add", "title": title, "spec": spec, "kind": task["kind"],
              "tier": hop_tier(task, fup), "priority": task["priority"], "budget_usd": task["budget_usd"],
              "continues": task["id"]}
    try:   # a hop that publishes a code task's branch: task_add makes it kind code; no copied tier
        pub = publishkind.resolve(db, task["kind"], spec)
    except ValueError:
        pub = None   # task_add refuses it with the same message
    if pub:
        want = str(fup.get("tier") or "").strip().lower()
        del action["budget_usd"], action["tier"]
        action.update({"tier": want} if want in TIERS else {})
    saved = db.kv(coord.NOTES_KEY)   # the coordinator's own notes for its next digest stay as they were
    try:
        problems = coord.apply(p, [action])
    finally:
        db.set_kv(coord.NOTES_KEY, saved)
    new = db.one("SELECT id FROM tasks WHERE parent=? AND title=? ORDER BY id DESC", (task["id"], title))
    if problems or not new:
        if refused is not None:
            refused.extend(problems)
        return None
    if not pub:
        copied = action["tier"] == (assigned_tier(task) if assigned_tier(task) in TIERS else "standard")
        db.update_task(new["id"], tier_source="parent_copy" if copied else "hop_followup")
    db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (time.time(), f"task:{task['id']}", AUTO_EVENT, "low", f"#{new['id']} {title} queued by the daemon ({why})",
          json.dumps({"root": ch[0]["id"], "next": new["id"], "hops": len(ch), "why": why}), "handled", task["id"]))
    return new["id"], why


PUSHQ_WITHDRAWN = ("cancelled", "rejected", "withdrawn")


def approved_or_landed(db: DB, task_id: int) -> bool:
    """Whether task `task_id`'s change was approved by a review or landed (on the push branch, or under other
    commits): its `continue:` hop is then a new task, never a re-queue. Judged over landed.carriers (the
    task, its reviews, their fixes and re-reviews, ...; DB reads only, no git), as pushq.approve keys a push
    queue row by the reviewing task: a row of any of them or of their branches not withdrawn, or a landing
    landed.recorded finds."""
    from . import landed
    carry = landed.carriers(db, task_id)
    ids, branches = list(carry), sorted({v[0] for v in carry.values() if v[0]})
    if db.one("SELECT 1 FROM push_queue WHERE (task IN (%s) OR branch IN (%s)) AND status NOT IN (%s) LIMIT 1"
              % (",".join("?" * len(ids)), ",".join("?" * len(branches)) or "NULL",
                 ",".join("?" * len(PUSHQ_WITHDRAWN))), [*ids, *branches, *PUSHQ_WITHDRAWN]):
        return True
    return bool(landed.recorded(db, task_id, carry)[0])


def budget_base(cfg: dict, task: dict) -> tuple[list, float]:
    """(task `task`'s labels, the per-task budget a re-queue tops it up to): its BUDGET_BASE_LABEL, else
    its budget now (or the tier default), then added to the labels returned."""
    from . import budget as bud
    try:
        labels = json.loads(task.get("labels") or "[]")
    except ValueError:
        labels = []
    labels = labels if isinstance(labels, list) else []
    base = next((float(lb[len(BUDGET_BASE_LABEL):]) for lb in labels if isinstance(lb, str)
                 and lb.startswith(BUDGET_BASE_LABEL) and re.fullmatch(r"\d+(?:\.\d+)?", lb[len(BUDGET_BASE_LABEL):])),
                None)
    if base is None:
        base = float(task.get("budget_usd") or bud.task_default_usd(cfg, task.get("tier")))
        labels.append(f"{BUDGET_BASE_LABEL}{base:g}")
    return labels, base


def requeue(p, cfg: dict, task: dict, fup: dict, why: str, attempts: int, now: float | None = None) -> bool:
    """Re-queue done code task `task` for `fup`, its lone `continue:` hop that hop_ready passed (`why`), when
    its change is neither approved nor landed (approved_or_landed): the same task, so its id, worktree,
    branch and review chain stay. The follow-up's spec is appended as an `## Update`, its per-task budget
    tops up to a fresh one (its BUDGET_BASE_LABEL, the budget it had before its first re-queue), its
    attempts go back to `attempts` (the count before this run) and its result (the last hand-off) stays.
    An AUTO_EVENT records it. Whether it was re-queued; False leaves the task as it was."""
    db = p.db
    if not caps(cfg)["requeue"] or task.get("kind") != "code" or not task.get("branch") \
            or approved_or_landed(db, task["id"]):
        return False
    now = time.time() if now is None else now
    labels, base = budget_base(cfg, task)
    title = str(fup.get("title") or "").strip()[:200]
    spec = str(fup.get("spec") or "").strip()
    update = f"## Update\nContinue ({title}), re-queued by the daemon after your last hand-off:\n{spec}"
    rows = db.conn.execute("UPDATE tasks SET status='queued', spec=?, labels=?, budget_usd=?, attempts=?, "
                           "not_before=NULL, blocked_reason=NULL, updated=? WHERE id=? AND status='done'",
                           (f"{str(task.get('spec') or '').rstrip()}\n\n{update}", json.dumps(
                               [*labels, f"{REQUEUED_LABEL}{int(now)}"]),
                            round(float(task.get("spent_usd") or 0) + base, 2), int(attempts), now,
                            task["id"])).rowcount
    if not rows:
        return False
    from . import dispatchguard
    dispatchguard.record_requeue(db, task["id"], "done", "continue")   # a new hop: its done count restarts
    ch = chain(db, task["id"])
    db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (now, f"task:{task['id']}", AUTO_EVENT, "low",
          f"#{task['id']} re-queued by the daemon for its continue {title!r} ({why})",
          json.dumps({"root": ch[0]["id"] if ch else task["id"], "next": task["id"], "requeued": True,
                      "hops": hop_count(ch), "why": why}), "handled", task["id"]))
    return True


def auto_continued(db: DB, task_id: int) -> bool:
    """Whether the daemon queued a new hop after task `task_id` (a re-queue of the task itself is none)."""
    return bool(db.one("SELECT 1 FROM events WHERE kind=? AND task=? AND COALESCE(json_extract(CASE WHEN "
                       "json_valid(data) THEN data END, '$.requeued'), 0) = 0", (AUTO_EVENT, task_id)))


def add_notice(db: DB, text: str) -> None:
    notes = [n for n in db.kv(NOTICES_KEY, []) or [] if isinstance(n, dict)]
    db.set_kv(NOTICES_KEY, (notes + [{"ts": time.time(), "text": text[:800]}])[-MAX_NOTICES:])


def notices(db: DB) -> list[str]:
    return [str(n.get("text") or "") for n in db.kv(NOTICES_KEY, []) or [] if isinstance(n, dict)]


def clear_notices(db: DB, before: float) -> None:
    """Drop the notices a turn that started at `before` saw; later ones wait for the next turn."""
    notes = [n for n in db.kv(NOTICES_KEY, []) or [] if isinstance(n, dict)]
    keep = [n for n in notes if float(n.get("ts") or 0) > before]
    if keep != notes:
        db.set_kv(NOTICES_KEY, keep or None)


def lineages(db: DB, since: float) -> list[dict]:
    """Each lineage with a hop active since `since`: {root, title, hops, usd, auto, refused, overridden, tip,
    fix_hops, fix_usd, cont_hops, cont_usd}: usd is the whole lineage's, the hop figures split its hops into
    review fixes (is_review_fix) and continuations, each with the hops' own spend."""
    out: dict[int, dict] = {}
    for t in db.q("SELECT * FROM tasks WHERE COALESCE(updated, created)>=? ORDER BY id", (since,)):
        if prev_of(t) is None:
            continue
        ch = chain(db, t["id"])
        if len(ch) < 2 or ch[0]["id"] in out and out[ch[0]["id"]]["hops"] >= len(ch) - 1:
            continue
        fixes = [t for t in ch[1:] if is_review_fix(t)]
        out[ch[0]["id"]] = {"root": ch[0]["id"], "title": str(ch[0]["title"]), "hops": len(ch) - 1,
                            "usd": spend(ch), "tip": ch[-1]["id"], "status": ch[-1]["status"],
                            "fix_hops": len(fixes), "fix_usd": spend(fixes),
                            "cont_hops": len(ch) - 1 - len(fixes), "cont_usd": spend(ch[1:]) - spend(fixes),
                            "auto": 0, "refused": 0, "overridden": 0}
    for kind, key in ((AUTO_EVENT, "auto"), (CAP_EVENT, "refused"), (OVERRIDE_EVENT, "overridden")):
        for r in db.q("SELECT data FROM events WHERE kind=? AND ts>=?", (kind, since)):
            try:
                root = int(json.loads(r["data"] or "{}").get("root"))
            except (ValueError, TypeError, AttributeError):
                continue
            if root in out:
                out[root][key] += 1
    return sorted(out.values(), key=lambda x: -x["usd"])


def audit_line(db: DB, since: float, top: int = 8) -> str:
    """The self-efficiency audit's line: hops and spend per lineage, split into continuations and review
    fixes (those the review-loop line counts already), and refusals; '' with none."""
    ls = lineages(db, since)
    if not ls:
        return ""
    refused = sum(x["refused"] for x in ls)
    cont, fix = [x for x in ls if x["cont_hops"]], [x for x in ls if x["fix_hops"]]
    return (f"continue chains: {len(ls)} lineage(s), {sum(x['hops'] for x in ls)} hops, "
            f"${sum(x['usd'] for x in ls):.2f} with their first tasks; continuations: "
            f"{sum(x['cont_hops'] for x in cont)} hops in {len(cont)} lineage(s), ${sum(x['cont_usd'] for x in cont):.2f}; "
            f"review fixes (also in the review-loop line): {sum(x['fix_hops'] for x in fix)} hops in {len(fix)} "
            f"lineage(s), ${sum(x['fix_usd'] for x in fix):.2f}; {sum(x['auto'] for x in ls)} hops queued by the daemon, "
            f"{refused} refused by a cap, {sum(x['overridden'] for x in ls)} passed with a reason: " + "; ".join(
                f"#{x['root']} {headline.clip(x['title'], 50)}: {x['hops']} hops"
                + (f" ({x['fix_hops']} review fix{'es' if x['fix_hops'] > 1 else ''})" if x["fix_hops"] else "")
                + f" to #{x['tip']} ({x['status']}), ${x['usd']:.2f}"
                + (f", {x['auto']} auto" if x["auto"] else "") + (f", {x['refused']} refused" if x["refused"] else "")
                + (f", {x['overridden']} overridden" if x["overridden"] else "") for x in ls[:top])
            + (f"; +{len(ls) - top} more" if len(ls) > top else ""))
