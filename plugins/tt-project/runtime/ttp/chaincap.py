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

Caps (`chains` in project.json; 0 turns one off): max_hops (DEFAULTS: 8), max_stalls (2 failed or
no-progress hops in a row) and max_usd (40 for the whole lineage). Once one is reached, or while
the lineage's area is under review.area_fail_cap (a reviewcap.REVIEW_AREA_EVENT within
reviewcap.AREA_WINDOW_S naming one of its tasks), a task_add that continues the lineage is refused,
and a CAP_EVENT makes the next coordinator turn a triggered one (TRIGGER). The only way past is
the action's `reason` naming what changed (REASON_RE: a new approach, a rescoped spec, new
evidence); that override is recorded as an OVERRIDE_EVENT.

Model-free hop (`chains.auto`, on by default): a done task whose only proposal is one `continue:`
follow-up, in a lineage below every cap, that made progress gets the follow-up queued by the daemon
with `continues` set (an AUTO_EVENT). A hop of a done task starts from the base, not its branch, with
one exception: a split. A code task whose branch has changes and that proposes a `continue:` hop, or
whose hand-off marks its head unfinished (marked_split), ended as a split (SPLIT_LABEL, split:<head>);
a `continue:` follow-up that runs only once the task has landed (post_landing: a deploy, live install or
check) carries nothing forward and makes no split:
its head is work in progress. It gets no review, and its hop takes the branch over (`continues:`, so
it starts from that head) and starts at once: a start_when `landed:` on a task of its own chain could
never pass, as those commits land only with the hop's own work (own_wait refuses it). The open tasks
that waited on the split task wait on the hop. The last hop, the first that ends without a split,
gets the chain's one review (split_hops). The notice, and a local_only notice for a branch its review
still carries, go to the next digest (NOTICES_KEY) without waking the coordinator. Anything else
still goes to it.

The daemon appends branch_brief to the queued hop's spec, model-free: the parent branch's head and
its changed files with their hunk line ranges against the base ref, so the hop starts with less
orientation. It is bounded (BRIEF_MAX_LINES) and left out when the parent has no branch here.
"""
from __future__ import annotations

import json
import re
import time

from . import headline
from .db import DB, continues_id, load_result

CONT_TITLE = re.compile(r"(?i)^\s*(?:continue|mechanical)\s*:")
# A hop that only runs checks, pushes or edits spec or PR text: queued at the light tier (hop_tier).
FIX_TITLE = re.compile(r"(?i)^\s*fix review #\d+")   # the daemon's review fix title, also used by hand
MECH_TITLE = re.compile(r"(?i)^\s*mechanical\s*:")
TIERS = ("light", "standard", "deep")
REASON_RE = re.compile(r"(?i)\b(?:new|different|changed|other)\s+(?:approach|evidence|design|strategy)\b"
                       r"|\bre-?scop\w*|\bnarrow\w*\s+(?:the\s+)?scope|\bscope[sd]?\s+(?:down|narrow\w*)")
MIN_REASON = 20
DEFAULTS = {"max_hops": 8, "max_stalls": 2, "max_usd": 40.0, "auto": True}
CAP_EVENT = "continue_cap"
OVERRIDE_EVENT = "continue_cap_override"
AUTO_EVENT = "continue_auto"
TRIGGER = "continue chain over its cap"
FOLLOWS = "follows:"   # label of a task_add that `continues` a done task: a lineage link that takes nothing over
NOTICES_KEY = "chain_notices"   # kv: [{ts, text}]: hops the daemon queued, shown once in the next digest
MAX_CHAIN = 200
MAX_NOTICES = 30
BRIEF_MAX_LINES = 40   # branch_brief's bound, its header and '+N more files' line included
BRIEF_MAX_RANGES = 8   # hunk ranges listed per file before '...'
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
SPLIT_LABEL = "split:"   # a done code task that ended as a split: split:<its head>
# A split hand-off's own words: a summary that opens "Split at/here/..." or names the split line, a head
# commit subject that says WIP.
SPLIT_SUMMARY = re.compile(r"(?i)^\W*split\b(?=\s*(?:at|here|mid|after|before|because|per|on|by|[(:;,.]|$))"
                           r"|\bpast the split line\b")
WIP_SUBJECT = re.compile(r"(?:^|[\s(\[])WIP(?=$|[\s):\]])|\(work in progress\)")
# A post-landing follow-up's own words (post_landing): 'after' or 'once' this task (it, this task or change,
# the change, #<id>) lands, and a live step in its title or that sentence; a title that lands, reviews,
# pushes, fixes or finishes the change still carries it forward.
AFTER_LANDING = re.compile(r"(?i)\b(?:after|once)\s+(?:it|this\s+(?:task|change)|the\s+change|#(\d+))\s+"
                           r"(?:lands|has\s+landed|is\s+landed|is\s+pushed|reaches\s+the\s+push\s+branch)\b")
SENTENCE_END = re.compile(r"[.!?;](?=\s|$)|\n")
LIVE_STEP = re.compile(r"(?i)\b(?:deploy\w*|(?:re-?)?install\w*|upgrad\w*|roll(?:s|ed|ing)?(?:\s+(?:it|them))?[\s-]?outs?"
                       r"|live\s+(?:check|test|run)\w*|verif\w*\b[^.;\n]{0,40}?\bon\s+(?:a|the|each|every|its)?\s*"
                       r"(?:host|box|machine|node)s?)\b(?!\.\w)")   # not a file name (install.sh)
CARRIES = re.compile(r"(?i)^\s*(?:continue|mechanical)\s*:\s*(?:then\s+|and\s+)?(?:re-?)?(?:land|review|push|fix"
                     r"|finish|complete)\w*\b|\b(?:land|review|push|fix|finish)(?:e?s)?\s+(?:it|this|the|its)\b")
WAIT_KEYS = ("start_when", "retry_when", "wait", "waiting_for")


def caps(cfg: dict) -> dict:
    c = cfg.get("chains") or {}
    out: dict = {"auto": c.get("auto", True) not in (False, 0, "off", "false")}
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
    """The task `task` continues: its `continues:` or FOLLOWS label, or the parent of a `continue:` follow-up."""
    if (c := continues_id(task)) is not None or (c := follows_id(task)) is not None:
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


def progress(p, ch: list[dict], i: int) -> str:
    """Why hop `ch[i]` made progress (see the module's rule), or '' when it did not."""
    from . import worktree
    hop = ch[i]
    if hop["status"] != "done":
        return ""
    try:
        base = worktree.resolve_base(p)
        refs = [b for t in ch[:i + 1] if (b := t.get("branch"))
                and worktree._git(p.root, "rev-parse", "--verify", "--quiet", f"{b}^{{commit}}", check=False)]
        if refs:
            n = worktree._git(p.root, "rev-list", "--count", f"--since=@{int(hop['created'] or 0)}", *refs,
                              f"^{base}", check=False)
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


def refusal(p, cfg: dict, ch: list[dict]) -> str:
    """Why another hop after the chain `ch` is refused, '' when it may follow."""
    if not ch:
        return ""
    c, root = caps(cfg), ch[0]
    hops, usd = len(ch) - 1, spend(ch)
    head = f"the continue chain of #{root['id']} ({str(root['title'])[:80]}) "
    if c["max_hops"] and hops + 1 > c["max_hops"]:
        return head + f"has {hops} hops; chains.max_hops is {c['max_hops']:g}"
    if c["max_usd"] and usd >= c["max_usd"]:
        return head + f"spent ${usd:.2f}; chains.max_usd is ${c['max_usd']:g}"
    if (a := area_held(p.db, ch)) is not None:
        return head + f"is in the review area of #{a}, under review.area_fail_cap (re-plan it first)"
    if c["max_stalls"] and (n := stalls(p, ch)) >= c["max_stalls"]:
        return head + f"ended with {n} failed or no-progress hops in a row; chains.max_stalls is {c['max_stalls']:g}"
    return ""


def reason_ok(reason) -> bool:
    """A reason that names what changed: a new approach, a rescoped spec or new evidence."""
    r = str(reason or "").strip()
    return len(r) >= MIN_REASON and bool(REASON_RE.search(r))


def check_add(p, cfg: dict, prev: int, reason) -> str:
    """For a task_add continuing `prev`: '' when its lineage is under every cap, a note when `reason`
    takes it past one; otherwise raises ValueError, with a CAP_EVENT for the
    next turn (once per lineage and hop count)."""
    db = p.db
    ch = chain(db, prev)
    why = refusal(p, cfg, ch)
    if not why:
        return ""
    data = {"root": ch[0]["id"], "tip": prev, "hops": len(ch) - 1, "usd": round(spend(ch), 2), "why": why}
    if reason_ok(reason):
        db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (time.time(), "coordinator", OVERRIDE_EVENT, "low", f"{why}; passed with reason: {str(reason)[:300]}",
              json.dumps({**data, "reason": str(reason)[:300]}), "handled", prev))
        return f"task_add continues #{prev} past its cap ({why}) with reason: {str(reason)[:200]}"
    fp = f"{CAP_EVENT}:{ch[0]['id']}:{len(ch) - 1}"
    if not db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
             (time.time(), "coordinator", CAP_EVENT, fp, "normal",
              f"{TRIGGER}: {why}. Another hop is refused. Re-plan: narrow the scope, change the approach, or stop "
              f"and report what is left; a task_add that continues it passes only with a `reason` naming what "
              f"changed.", json.dumps(data), "queued", prev))
    hint = "its `reason` does not name what changed (a new approach, a rescoped spec, new evidence)"
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
    its start_when or wait names the landing (waits_landing, decisive: such a task can never carry the
    work to its landing), or its title and spec say it runs after or once the task lands (AFTER_LANDING,
    no other task id in that phrase) and name a live step (LIVE_STEP), with no title that lands,
    reviews, pushes, fixes or finishes the change (CARRIES). The live step counts only in the title or in
    the sentence that says so, never anywhere in the spec."""
    fup = fup if isinstance(fup, dict) else {}
    if any(waits_landing(fup.get(k), task_id) for k in WAIT_KEYS):
        return True
    title = str(fup.get("title") or "")
    if CARRIES.search(title):
        return False
    text = f"{title}\n{fup.get('spec') or ''}"
    for m in AFTER_LANDING.finditer(text):
        if m.group(1) and m.group(1) != str(task_id):
            continue
        start = max((e.end() for e in SENTENCE_END.finditer(text, 0, m.start())), default=0)
        end = SENTENCE_END.search(text, m.end())
        if LIVE_STEP.search(title) or LIVE_STEP.search(text[start:end.start() if end else len(text)]):
            return True
    return False


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
    """The tier of the hop queued for `fup`: the parent's, or lower when the follow-up asks for it with
    `"tier"` or a `mechanical:` title (light). Never above the parent's: a hop that needs more is
    retried a tier up by the daemon."""
    tier = task["tier"] if task.get("tier") in TIERS else "standard"
    want = "light" if MECH_TITLE.match(str(fup.get("title") or "")) else str(fup.get("tier") or "").strip().lower()
    return want if want in TIERS and TIERS.index(want) < TIERS.index(tier) else tier


def branch_brief(p, task: dict) -> str:
    """The block the daemon appends to a hop's spec: `task`'s branch head and its changed files, each
    with its hunk line ranges (on the head; `-N`: lines deleted after line N) against the merge base with
    the base ref, at most BRIEF_MAX_LINES lines. '' when the task has no branch, the branch is missing
    or git cannot read it: the hop is queued without it."""
    from . import landed, worktree
    branch = str(task.get("branch") or "").strip()
    if not branch:
        return ""
    try:
        repo = p.root
        head = landed._commit(repo, f"refs/heads/{branch}")
        if not head:
            return ""
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


def auto_hop(p, cfg: dict, task: dict, fup: dict, why: str | None = None) -> tuple[int, str] | None:
    """Queue `fup` when hop_ready (`why`, when the caller checked it already): (new task id, why it
    progressed), else None. The hop of a split task (split_head) takes its branch over and starts at
    once; it never waits on that task's landing, which comes only with the hop's own work."""
    from . import coordinator as coord
    db = p.db
    why = hop_ready(p, cfg, task, fup) if why is None else why
    if not why:
        return None
    ch = chain(db, task["id"])
    title = str(fup["title"]).strip()[:200]
    spec = str(fup.get("spec") or "")
    if brief := branch_brief(p, task):
        spec = f"{spec.rstrip()}\n\n{brief}" if spec.strip() else brief
    action = {"type": "task_add", "title": title, "spec": spec, "kind": task["kind"],
              "tier": hop_tier(task, fup), "priority": task["priority"], "budget_usd": task["budget_usd"],
              "continues": task["id"]}
    saved = db.kv(coord.NOTES_KEY)   # the coordinator's own notes for its next digest stay as they were
    try:
        problems = coord.apply(p, [action])
    finally:
        db.set_kv(coord.NOTES_KEY, saved)
    new = db.one("SELECT id FROM tasks WHERE parent=? AND title=? ORDER BY id DESC", (task["id"], title))
    if problems or not new:
        return None
    db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (time.time(), f"task:{task['id']}", AUTO_EVENT, "low", f"#{new['id']} {title} queued by the daemon ({why})",
          json.dumps({"root": ch[0]["id"], "next": new["id"], "hops": len(ch), "why": why}), "handled", task["id"]))
    return new["id"], why


def auto_continued(db: DB, task_id: int) -> bool:
    return bool(db.one("SELECT 1 FROM events WHERE kind=? AND task=?", (AUTO_EVENT, task_id)))


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
