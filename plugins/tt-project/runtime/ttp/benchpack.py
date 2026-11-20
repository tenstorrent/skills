"""Context packs and the original-change arm for the code benches (bench v2).

A bench cell used to get only the task's spec. The original worker had much more at dispatch: the worker prompt
the harness built (coordinator context, memory, charter, project docs), the tasks and user messages the spec
cites, the review findings for a fix task, and what it read from live systems while it worked. `build_pack`
rebuilds that from the original run's own records (its prompt, transcript and the project's task records, all
read-only) into one text, pinned by its sha256. Every arm and tool of a task gets the same pack.

`classify` says whether a task can be a code-bench task at all: `code-bench`, `live-only` (the original worker
needed live access a snapshot cannot give: remote or device runs, or most acceptance items need the live system)
or `invalid` (no usable record, or the pack names the fix). Packs hold project data: they live under the bench's
own folder, never in a repository.

The original change itself is the unlabeled `original` arm of every blind batch (`add_original`): the landed
diff graded like any arm's. A task whose original does not pass is dropped and listed (`split_original`).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from .project import durable_write

CODE, LIVE_ONLY, INVALID = "code-bench", "live-only", "invalid"
ORIGINAL = "original"
ORIGINAL_ARM = {"provider": "original", "model": "landed-change", "effort": ""}

# Sections of the worker prompt that tell a worker how to operate the harness (progress notes, hand-off file,
# machine locks): they mean nothing in a bench cell. Everything else stays, so the pack is what the worker read.
DROP_HEADS = ("Progress", "Working in parallel", "Shared clusters", "Updates mid-task", "Handoff", "Budget and time",
              "KNOWN PATHS", "FORKED FROM", "Landing on the project's branch", "Pull requests")
PREAMBLE_END = "# BINDING RESTRICTIONS"
# The delivery steps of the original worker (land, push, open a PR, fetch): a cell has no remote and its agent may
# not use one, and the cheat audit rightly flags them, so no arm's pack carries a step that orders one. The same
# cut for every arm. Bump PACK_VERSION when this changes: a pack built under an older one is rebuilt at the
# next run boundary (attach), never inside a run.
DELIVERY = re.compile(r"\bgh\s+(?:pr|api|repo)\b|\bttp\s+(?:push|express)\b|\bgit\s+(?:push|fetch|pull|clone|ls-remote)\b"
                      r"|\bgit\s+remote\s+add\b|\bgit\s+log\s+--all\b")
PACK_VERSION = 2

# A command whose output is a read of live state: its result goes into the pack as the original run saw it.
SNAP = re.compile(r"\bttp\s+(?:status|task|tasks|list|schedules|memory|config|runs|events|audit|paths|said|machines|"
                  r"checks\s+--?(?:list|status))\b|\bjournalctl\b|\bcrontab\s+-l\b|\bsystemctl\s+(?:status|list-\S+|show|cat)\b"
                  r"|\bsqlite3\b|\b(?:tail|cat|head|grep|rg|less|wc|ls)\b[^|;&]*(?:\.log\b|\.jsonl\b|/state/|/runs/|\.tt-project)"
                  r"|\b(?:ps|df|uptime|free|ss|lsof|pgrep)\b|\bcurl\b|\bgh\s+(?:issue|pr\s+view|api)\b")
# A command that ran something on another machine or a device: what the change was checked against cannot be
# snapshotted, so the task cannot be replayed in a cell.
REMOTE = re.compile(r"(?:^|[;&|(]|\bsudo|\btimeout\s+\d+)\s*(?:ssh|scp|rsync\s+(?:-\S+\s+)*[\w.@-]+:|srun|sbatch|salloc|kubectl|docker|tt-smi|"
                    r"tt-topology|ttp\s+lock)(?![\w-])", re.M)   # at the start of a command, not a word in an argument
NEEDS_LIVE_SHARE = 0.5   # acceptance lines needing the live system, over which the task is live-only
SNAP_ITEM, SNAP_TOTAL = 5000, 60000
REF_ITEM = 3500

INTRO = ("Context pack: what the engineer who first did this task had when it was handed over (the worker prompt "
         "its harness built, the tasks and user messages it cites, and what its run read from live systems, "
         "recorded then). Use it as background; the task below is what you do.")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def clip(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    head = n * 2 // 3
    return f"{text[:head]}\n[... {len(text) - n} characters cut ...]\n{text[-(n - head):]}"


def redact(text: str) -> str:
    from . import benchreplay
    return benchreplay.redact(text)


# -- the original run's records

def strip_prompt(text: str) -> str:
    """The worker prompt as the worker read it: the harness's fork preamble and its operating rules (progress,
    hand-off file, machine locks) cut, the restrictions, charter, corrections, memory and the task kept."""
    i = text.find(PREAMBLE_END)
    text = text[i:] if i >= 0 else text
    out, drop, fence = [], None, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"(#{1,3})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            if drop is not None and level <= drop:
                drop = None
            if drop is None and any(m.group(2).startswith(d) for d in DROP_HEADS):
                drop = level
        if drop is None:
            out.append(line)
    return strip_delivery("\n".join(out)).strip() + "\n"


def _units(text: str) -> list[list[str]]:
    # Blocks the delivery cut works on: a fenced code block, a heading line, a list item with its continuation
    # lines, or a paragraph; a blank line ends a unit and belongs to it.
    units: list[list[str]] = []
    fence = False
    for line in text.splitlines():
        s = line.lstrip()
        if fence:
            units[-1].append(line)
            fence = not s.startswith("```")
            continue
        open_ = bool(units) and units[-1][0].strip() != "" and units[-1][-1].strip() != "" \
            and not units[-1][0].lstrip().startswith(("#", "```"))
        if s and open_ and not s.startswith(("#", "```")) and not re.match(r"(?:[-*+]|\d+[.)])\s", s):
            units[-1].append(line)
        elif not s and units:
            units[-1].append(line)
        else:
            units.append([line])
            fence = s.startswith("```")
    return units


def strip_delivery(text: str) -> str:
    """`text` without the units (list item, paragraph, code block) that order a delivery step (DELIVERY). The
    binding restrictions block, up to the next top-level heading, is kept as written: it forbids, it does not order."""
    head, rest = "", text
    if text.startswith(PREAMBLE_END):
        m = re.search(r"\n# (?!BINDING)", text)
        head, rest = (text[:m.start() + 1], text[m.start() + 1:]) if m else (text, "")
    kept = ["\n".join(u) for u in _units(rest) if not DELIVERY.search("\n".join(u))]
    return head + "\n".join(kept)


def original_run(db, task_id: int) -> dict | None:
    """The first worker run of the task that left a prompt behind: what the worker was handed at dispatch."""
    for r in db.q("SELECT id, dir, session_id FROM runs WHERE task=? AND role='worker' ORDER BY id", (task_id,)):
        if r["dir"] and (Path(r["dir"]) / "prompt.md").exists():
            return dict(r)
    return None


def calls(output: Path):
    """[(tool name, input, result text)] in order from a stream-json transcript."""
    pending: dict[str, tuple[str, dict]] = {}
    got = []
    for line in output.read_text(errors="replace").splitlines() if output.exists() else []:
        try:
            j = json.loads(line)
        except ValueError:
            continue
        msg = j.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                pending[b.get("id", "")] = (str(b.get("name", "")), b.get("input") or {})
            elif b.get("type") == "tool_result":
                name, inp = pending.pop(b.get("tool_use_id", ""), ("", {}))
                c = b.get("content")
                text = c if isinstance(c, str) else "\n".join(str(x.get("text", "")) for x in c or [] if isinstance(x, dict))
                got.append((name, inp, text.strip()))
    return got


def snapshot(output: Path) -> tuple[list[dict], list[str]]:
    """(snapshot items {label, text}, remote commands) from the original transcript: the outputs of commands that
    read live state (logs, the project's records, processes, services) as the run saw them, and the commands that
    ran on another machine or a device."""
    items, remote, total = [], [], 0
    for name, inp, text in calls(output):
        cmd = str(inp.get("command") or "") if name == "Bash" else ""
        path = str(inp.get("file_path") or "") if name in ("Read", "Grep") else ""
        if cmd and REMOTE.search(cmd):
            remote.append(cmd.strip().splitlines()[0][:120])
            continue
        live = (cmd and SNAP.search(cmd)) or (path and re.search(r"/state/|\.tt-project|\.log$|\.jsonl$", path))
        if not live or not text or total >= SNAP_TOTAL:
            continue
        text = clip(redact(text), SNAP_ITEM)
        total += len(text)
        items.append({"label": cmd.strip()[:300] if cmd else f"Read {path}", "text": text})
    return items, remote


REF_TASK = re.compile(r"(?<![\w&])#(\d{3,6})\b")
REF_MSG = re.compile(r"(?i)\b(?:user\s+)?(?:msgs?|messages?)\s*#(\d+)((?:\s*(?:,|and|&|/)\s*(?:msgs?\s*)?#\d+)*)")


def refs(spec: str, title: str, own: int) -> tuple[list[int], list[int]]:
    """(task ids, user message ids) the task cites, each earlier than the task, in first-seen order. A fix task's
    title names the review it fixes ('Fix review #N')."""
    text = f"{title}\n{spec}"
    msgs: list[int] = []
    for m in REF_MSG.finditer(text):
        msgs += [int(m.group(1))] + [int(x) for x in re.findall(r"#(\d+)", m.group(2))]
    spans = [(m.start(), m.end()) for m in REF_MSG.finditer(text)]
    tasks: list[int] = []
    for m in REF_TASK.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        n = int(m.group(1))
        if n < own and n not in tasks:
            tasks.append(n)
    return tasks, [n for i, n in enumerate(msgs) if n not in msgs[:i]]


def _summary(result: str | None) -> str:
    try:
        j = json.loads(result or "")
        return str(j.get("summary") or result)
    except (ValueError, AttributeError):
        return str(result or "")


def _ts(t: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def referenced(db, task_ids: list[int], msg_ids: list[int], max_items: int = 12) -> tuple[list[str], dict]:
    """(markdown blocks, {"tasks": [...], "messages": [...]}) for the cited tasks (title, status, result summary)
    and user messages, inline."""
    blocks, used = [], {"tasks": [], "messages": []}
    for i in task_ids[:max_items]:
        for r in db.q("SELECT id, title, status, result FROM tasks WHERE id=?", (i,)):
            blocks.append(f"### Task #{r['id']}: {r['title']} [{r['status']}]\n\n{clip(_summary(r['result']), REF_ITEM)}\n")
            used["tasks"].append(i)
    for i in msg_ids[:max_items]:
        for r in db.q("SELECT id, ts, text FROM messages WHERE id=? AND direction='in'", (i,)):
            blocks.append(f"### User message #{r['id']} ({_ts(r['ts'])})\n\n{clip(str(r['text']), REF_ITEM)}\n")
            used["messages"].append(i)
    return blocks, used


def review_findings(db, title: str) -> str:
    """For 'Fix review #N: ...' tasks, the findings of review #N as its record states them."""
    m = re.match(r"(?i)\s*fix review #(\d+)", title or "")
    if not m:
        return ""
    for r in db.q("SELECT result FROM tasks WHERE id=?", (int(m.group(1)),)):
        return clip(_summary(r["result"]), 2 * REF_ITEM)
    return ""


# -- the pack

def acceptance_live(spec: str) -> tuple[int, int]:
    """(acceptance lines that need the live system, all acceptance lines)."""
    from . import benchrun
    lines = [x for x in benchrun.criteria(spec).splitlines() if x.strip()]
    return sum(1 for x in lines if benchrun.NOT_CHECKABLE in x), len(lines)


def build_pack(db, task: dict) -> dict:
    """{"class", "why", "text", "sha256", "sources"} for one bench task dict (task: source_task, title, spec, head).
    The text is empty when the task is not code-bench."""
    src = int(task["source_task"])
    run = original_run(db, src)
    if run is None:
        return {"class": INVALID, "why": "no recorded worker prompt for the task", "text": "", "sha256": "", "sources": {}}
    d = Path(run["dir"])
    prompt = strip_prompt((d / "prompt.md").read_text(errors="replace"))
    snap, remote = snapshot(d / "output.jsonl")
    snap = [x for x in snap if not DELIVERY.search(x["label"])]   # a delivery step read is no more context than its order
    if remote:
        return {"class": LIVE_ONLY, "why": f"the original run ran on another machine or a device: {remote[0]}",
                "text": "", "sha256": "", "sources": {"run": run["id"]}}
    live, total = acceptance_live(task.get("spec") or "")
    if total and live / total > NEEDS_LIVE_SHARE:
        return {"class": LIVE_ONLY, "why": f"{live} of {total} acceptance lines need the live system",
                "text": "", "sha256": "", "sources": {"run": run["id"]}}
    tids, mids = refs(str(task.get("spec") or ""), str(task.get("title") or ""), src)
    blocks, used = referenced(db, tids, mids)
    findings = review_findings(db, str(task.get("title") or ""))
    parts = [f"# Context pack\n\n{INTRO}\n", "## Worker prompt (as dispatched)\n\n" + prompt]
    if blocks:
        parts.append("## Tasks and user messages the task cites\n\n" + "\n".join(blocks))
    if findings:
        parts.append("## Review findings to fix\n\n" + findings + "\n")
    if snap:
        parts.append("## What the original run read from live systems (recorded then)\n\n" + "\n".join(
            f"### $ {s['label']}\n\n```\n{s['text']}\n```\n" for s in snap))
    text = redact("\n".join(parts))
    head = str(task.get("head") or "")
    if len(head) >= 9 and head[:9] in text:
        # A snapshot item or a cited record that quotes the fix commit's sha (git log, a push log) leaks
        # the answer's identity; the leak check drops that item instead of the task, so the pack keeps its
        # context. A cited task or message is kept with only the sha masked.
        bad = [i for i, s in enumerate(snap) if head[:9] in s["text"] or head[:9] in s["label"]]
        if bad:
            snap = [s for i, s in enumerate(snap) if i not in bad]
        blocks = [re.sub(re.escape(head[:11]), "the fix commit's sha", b) for b in blocks]
        parts = [f"# Context pack\n\n{INTRO}\n", "## Worker prompt (as dispatched)\n\n" + prompt]
        if blocks:
            parts.append("## Tasks and user messages the task cites\n\n" + "\n".join(blocks))
        if findings:
            parts.append("## Review findings to fix\n\n" + findings + "\n")
        if snap:
            parts.append("## What the original run read from live systems (recorded then)\n\n" + "\n".join(
                f"### $ {s['label']}\n\n```\n{s['text']}\n```\n" for s in snap))
        text = redact("\n".join(parts))
    if len(head) >= 9 and head[:9] in text:
        return {"class": INVALID, "why": "the pack names the fix commit", "text": "", "sha256": "",
                "sources": {"run": run["id"]}}
    return {"class": CODE, "why": "", "text": text, "sha256": _sha(text),
            "sources": {"run": run["id"], **used, "snapshot": len(snap), "review_findings": bool(findings)}}


def attach(task: dict, db, bench_dir: Path, keep: dict | None = None) -> dict:
    """The task's pack written beside its workspace (`pack-<sha12>.md` and pack.json, built once and reused) and
    the task dict with `pack` ({path, sha256, version, ...}) and `pack_class`. A task that is not code-bench carries
    `pack_class` and `pack_why` and no pack. A pack built under an older PACK_VERSION is rebuilt here, at the start
    of a run; its file stays where it is, so cells still running on it keep reading it. `keep`: {task: sha256} of
    the packs the cells already run in this result set used: that pack is reused (never a newer one beside it), so
    the arms of one repeat set never mix packs."""
    here = bench_dir / str(task["task"])
    meta = here / "pack.json"
    info = json.loads(meta.read_text()) if meta.exists() else None
    if info is not None:
        info.setdefault("file", "pack.md")
        if info["class"] == CODE and not (here / info["file"]).exists():
            info = None
    if want := (keep or {}).get(str(task["task"])):
        for f in sorted(here.glob("pack*.md")):
            if _sha(f.read_text()) == want:   # a pack a cell of this result set already ran on
                same = info is not None and info.get("sha256") == want
                info = {"class": CODE, "why": "", "sha256": want, "file": f.name,
                        "version": info.get("version", 1) if same else 1, "sources": info.get("sources", {}) if same else {}}
                break
        else:
            want = None
    if info is None or (not want and info.get("version", 1) < PACK_VERSION):
        info = build_pack(db, task)
        info["version"] = PACK_VERSION
        here.mkdir(parents=True, exist_ok=True)
        if info["text"]:
            info["file"] = f"pack-{info['sha256'][:12]}.md"
            durable_write(here / info["file"], info["text"])
        durable_write(meta, json.dumps({k: v for k, v in info.items() if k != "text"}, indent=1) + "\n")
    out = {**task, "pack_class": info["class"], "pack_why": info.get("why", "")}
    if info["class"] == CODE:
        out["pack"] = {"path": str(here / info["file"]), "sha256": info["sha256"], "version": info.get("version", 1),
                       "sources": info.get("sources", {})}
    return out


class PackError(Exception):
    pass


def read(task: dict) -> str:
    """The task's pack text, checked against its pinned hash; "" when the task has none."""
    p = task.get("pack")
    if not p:
        return ""
    text = Path(p["path"]).read_text()
    if _sha(text) != p["sha256"]:
        raise PackError(f"the context pack of {task['task']} does not match its pinned sha256")
    return text


def frame(pack: str, prompt_text: str) -> str:
    """The cell prompt with the pack ahead of the task."""
    return f"{pack}\n\n---\n\n{prompt_text}" if pack else prompt_text


def screen(tasks: dict, dbs: dict, bench_dir: Path, keep: dict | None = None) -> tuple[dict, list[dict]]:
    """(code-bench tasks with their packs attached, [{project, task, class, why}] for those left out), `dbs`:
    project name -> read-only db."""
    kept, left = {}, []
    for tid, t in tasks.items():
        got = attach(t, dbs[t["project"]], bench_dir, keep)
        if got["pack_class"] == CODE:
            kept[tid] = got
        else:
            left.append({"project": t["project"], "task": tid, "source_task": t.get("source_task"),
                         "class": got["pack_class"], "why": got["pack_why"]})
    return kept, left


# -- the original-change arm

def add_original(out: Path, tasks: dict, lock: threading.Lock | None = None) -> list[str]:
    """Give every task the original landed change as one more cell (arm `original`): its diff from base to head
    in the bench's mirror, no agent run, no cost. Returns the task ids written. Idempotent."""
    from . import benchdaily
    lock = lock or threading.Lock()
    path = out / "results.jsonl"
    have = benchdaily.read_rows(path)
    done = []
    for tid, t in tasks.items():
        if (tid, 0, ORIGINAL) in have:
            continue
        keep = out / "cells" / f"{tid}-r0-{ORIGINAL}"
        keep.mkdir(parents=True, exist_ok=True)
        diff = benchdaily._git(Path(t["repo"]), "diff", "--no-color", t["base"], t["head"])
        durable_write(keep / "diff.patch", diff)
        benchdaily.append_row(path, {"task": tid, "rep": 0, "arm": ORIGINAL, **ORIGINAL_ARM, "outcome": "done",
                                     "passed": True, "cost_usd": 0.0, "wall_s": 0, "session_id": ""}, lock)
        done.append(tid)
    return done


def split_original(cells: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """(cells of the tasks whose original passed, dropped [{task, why}], pass rate {passed, n, per_task}).
    A task whose original change did not pass is dropped from the table and listed: it cannot tell the arms apart."""
    orig = {c["task"]: c for c in cells if c["arm"] == ORIGINAL}
    bad = {t for t, c in orig.items() if not c["landed"]}
    dropped = [{"task": t, "project": orig[t].get("project", ""),
                "why": f"the original change did not pass the blind review (requirements {orig[t]['criteria']}, "
                       f"correctness {orig[t]['correctness']})"} for t in sorted(bad)]
    rate = {"n": len(orig), "passed": len(orig) - len(bad),
            "per_task": {t: bool(c["landed"]) for t, c in sorted(orig.items())}}
    return [c for c in cells if c["task"] not in bad and c["arm"] != ORIGINAL], dropped, rate
