"""Blind reviews: a review or grading run that must not learn which model, provider, effort or model family made
the work it judges (the device A/B and shadow reviews, and any task created with BLIND_LABEL).

The flag is structured: the task that creates a blind review adds BLIND_LABEL to its labels, the daemon gives
its runs TTP_BLIND=1 (every provider: it is the run's environment), and `ttp` reads either. For such a caller
`ttp` runs only the commands in ALLOWED; `task show` and `task list` drop the model, provider, effort, pin,
spend and arm fields and labels and mask model names in the text they print. The coordinator, normal workers
and stats are not blind callers and keep the full data.

A review reads the reviewed run's files from a masked packet (`packet`), never the run folder (run.json and the
transcript name the model). Device shadow reviews name the arm by an opaque code (`code_for`), which the report
maps back (`arm_of`)."""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from .util import task_labels

BLIND_LABEL = "blind_review"
GRADER_EFFORT = "high"   # the most effort a grader runs at (a grader is not an action: max only costs)
# Labels of the reviews the daemon queues for device work (deviceab and deviceshadow: first and second
# reviewer). Each names its reviewer slot and subject, so two of them differ when their labels do, whatever
# their titles say (supersede.duplicate_of), and they are not the coordinator's new tasks (coordinator.tasks_made).
DAEMON_REVIEW_PREFIXES = ("device_ab_review", "device_shadow_review")
# A blind device reviewer's read budget: unbounded log reading made second reviews 3-10x the first's turns.
READ_BUDGET = ("Read budget: about 15 tool calls in total. Read result.json, runs.md and the run summary first; "
               "grep logs with short context, never re-render whole transcripts; spot-check at most 3 cited "
               "facts against the logs.")
ENV = "TTP_BLIND"
CODES_KV = "blind_review_codes"   # {code: "<tid>:<arm>"}

# What a blind caller may run: hand-off, notes and reads of its own task; nothing that reports runs, models,
# spend or stats.
ALLOWED = {"cmd_note", "cmd_progress", "cmd_notify", "cmd_upstream", "cmd_task", "cmd_detach", "cmd_lock",
           "cmd_clip", "cmd_hours", "cmd_alive", "cmd_landed"}
TASK_ACTIONS = {"show", "list"}

# Fields that name or hint at the arm: the model, provider and effort pins, the account, and spend (an open
# model on a free plan shows as $0) and the run's record id, which orders it against runs outside the packet.
MASK_KEYS = {"model", "provider", "effort", "pin_verify", "account", "spent_usd", "budget_usd", "usd", "cost_usd",
             "called", "models", "providers", "arm", "arms", "run_id", "run_dir", "code_arm"}
ARM_LABELS = ("device_ab", "device_shadow", "model:", "provider:", "effort:", "pin:", "route:", "jev")

# Model ids and family words. Word-bounded and case-insensitive; a path such as `.claude/` is left alone (the
# lookarounds refuse a word next to `.`, `/` or `-` path parts) so masked text stays readable.
_FAMILIES = (r"claude(?:-[a-z0-9.]+)*|opus|sonnet|haiku|fable|glm[-a-z0-9.]*|gpt-?[a-z0-9.]*|"
             r"gemini[-a-z0-9.]*|qwen[-a-z0-9.]*|deepseek[-a-z0-9.]*|gemma[-a-z0-9.]*|llama[-a-z0-9.]*|"
             r"mistral[-a-z0-9.]*|kimi[-a-z0-9.]*|minimax[-a-z0-9.]*|codex|cursor|anthropic|openai|zhipu|"
             r"chatglm|z\.ai")
WORDS = re.compile(rf"(?<![\w./-])(?:{_FAMILIES})(?![\w/])", re.I)
MASK = "[model]"
PACKET_MAX = 2_000_000   # bytes per hand-off in a packet, and for all its runs' transcripts (or progress) together

# The arm words a masked dict key carries: a usage map keyed by a model id must not keep that id as its key.
# Each masked key keeps its kind (model:), so two different keys never collide; the numbering is per
# packet (or per top-level mask() call), so a packet's names never depend on what was masked before it.


def scrub(text) -> str:
    """`text` with every model id and family word masked."""
    return WORDS.sub(MASK, str(text or ""))


def _arm_label(lb: str) -> bool:
    return lb.startswith(ARM_LABELS)


def _mask_key(k, names: dict) -> str:
    """A dict key whose text carries arm words keeps its kind but not its identity: the key is
    replaced by a masked name numbered per kind in `names` (one packet's), so two different model ids
    never collide into one key and the same id gets the same name throughout the packet."""
    k = str(k)
    if WORDS.search(k) or any(t in k.lower() for t in ("provider:", "effort:")):
        if k not in names:
            kind = "model" if WORDS.search(k) else "value"
            names[k] = f"[{kind}:{sum(v.startswith(f'[{kind}:') for v in names.values()) + 1}]"
        return names[k]
    return k


def mask(v, key: str = "", names: dict | None = None):
    """A JSON-like value with arm fields dropped, arm labels dropped and model names masked in its strings.
    `names`: the masked key names given so far in this packet (a new numbering when None)."""
    names = {} if names is None else names
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if str(k) in MASK_KEYS:
                continue
            nk = _mask_key(k, names)
            if k == "labels" and isinstance(x, str):   # the task row keeps labels as a JSON string
                try:
                    x = json.dumps([lb for lb in json.loads(x or "[]") if not _arm_label(str(lb))])
                except ValueError:
                    x = "[]"
                out[nk] = scrub(x)
                continue
            if k == "result" and isinstance(x, str):   # a stored hand-off: masked as JSON where it parses
                try:
                    out[nk] = json.dumps(mask(json.loads(x), names=names))
                except ValueError:
                    out[nk] = scrub(x)
                continue
            out[nk] = mask(x, str(k), names)
        return out
    if isinstance(v, list):
        if key == "labels":
            v = [lb for lb in v if not _arm_label(str(lb))]
        return [mask(x, names=names) for x in v]
    if isinstance(v, str):
        return scrub(v)
    return v


def review_slots(task: dict | None) -> set[str]:
    """The daemon review labels `task` carries (its reviewer slot and subject); empty for any other task."""
    return {lb for lb in task_labels((task or {}).get("labels")) if lb.startswith(DAEMON_REVIEW_PREFIXES)}


def is_blind_task(task: dict | None) -> bool:
    return bool(task) and BLIND_LABEL in task_labels(task.get("labels"))


def cap_effort(effort: str | None) -> str:
    """A grader's effort: unset is GRADER_EFFORT, anything above GRADER_EFFORT (xhigh, max) runs at it."""
    from .failover import EFFORT_RANK
    e = str(effort or "").strip().lower()
    if not e:
        return GRADER_EFFORT
    return GRADER_EFFORT if e in EFFORT_RANK and EFFORT_RANK.index(e) > EFFORT_RANK.index(GRADER_EFFORT) else e


def env(task: dict | None) -> dict[str, str]:
    """The run environment's flag for a blind task (empty otherwise)."""
    return {ENV: "1"} if is_blind_task(task) else {}


def caller_blind(env_: dict | None = None) -> bool:
    """Whether this `ttp` call comes from a blind review: TTP_BLIND, or its task (TTP_TASK in the project at
    TTP_PROJECT) carries BLIND_LABEL. The label check holds even where a provider drops the env flag."""
    e = os.environ if env_ is None else env_
    if e.get(ENV) == "1":
        return True
    tid, base = str(e.get("TTP_TASK") or ""), e.get("TTP_PROJECT") or ""
    if not tid.isdigit() or not base or not (Path(base) / "state" / "project.db").exists():
        return False
    try:
        from .project import Project
        return is_blind_task(Project(base).db.task(int(tid)))
    except Exception:
        return False


def refusal(fn_name: str, action: str = "") -> str | None:
    """Why a blind caller may not run this command, else None."""
    if fn_name not in ALLOWED:
        return "a blind review cannot run this ttp command (it could show which model did the work)"
    if fn_name == "cmd_task" and action not in TASK_ACTIONS:
        return "a blind review can only `ttp task show` or `ttp task list`"
    return None


def code_for(db, tid: int, arm: str) -> str:
    """The opaque code naming (tid, arm) in a blind review's title and label; made once, kept in kv."""
    codes = db.kv(CODES_KV, {}) or {}
    want = f"{int(tid)}:{arm}"
    for code, v in codes.items():
        if v == want:
            return code
    code = "s" + secrets.token_hex(4)
    db.set_kv(CODES_KV, {**codes, code: want})
    return code


def arm_of(q, code: str) -> str:
    """The arm a code names; an older label that named the arm itself passes through."""
    rows = q("SELECT value FROM kv WHERE key=?", (CODES_KV,))
    try:
        codes = json.loads(rows[0]["value"]) if rows else {}
    except (TypeError, ValueError, KeyError):
        codes = {}
    v = codes.get(code) if isinstance(codes, dict) else None
    return v.partition(":")[2] if isinstance(v, str) else code


def _jsonl(raw: str, names: dict | None = None) -> str:
    out, names = [], {} if names is None else names
    for line in raw.splitlines():
        try:
            out.append(json.dumps(mask(json.loads(line), names=names)))
        except ValueError:
            out.append(scrub(line))
    return "\n".join(out) + ("\n" if out else "")


def _write(path: Path, text: str) -> None:
    from .project import durable_write
    durable_write(path, text)


PACKET_FILES = ("result.json", "progress.md", "output.jsonl")
HEAD_SHARE = 0.25   # of a trimmed file's share: its head; the rest keeps its tail (the final messages)


def run_dirs(db, tid: int) -> list[str]:
    """Every run folder of task `tid`, oldest first: a waiting hand-off and its resume, a compaction resume and
    each device leg are runs of their own, and a blind reviewer sees them all (never a coordinator run)."""
    runs = Path(db.path).parent / "runs"
    out = []
    for r in db.q("SELECT id, dir FROM runs WHERE task=? AND role!='coordinator' ORDER BY id", (int(tid),)):
        d = Path(r["dir"] or runs / str(r["id"]))
        if d.is_dir():
            out.append(str(d))
    return out


def _shares(sizes: list[int], total: int) -> list[int]:
    """Each file's byte share of `total`: a small file keeps all of it, the rest split what is left evenly."""
    out, left = [0] * len(sizes), total
    for k, i in enumerate(sorted(range(len(sizes)), key=lambda i: sizes[i])):
        out[i] = min(sizes[i], left // (len(sizes) - k))
        left -= out[i]
    return out


def _last_line(fh, size: int) -> int:
    """Where the file's last line starts (a trailing newline ends it, it does not start an empty one)."""
    end = size
    if size:
        fh.seek(size - 1)
        end -= fh.read(1) == b"\n"
    pos = end
    while pos > 0:
        n = min(65536, pos)
        fh.seek(pos - n)
        i = fh.read(n).rfind(b"\n")
        if i >= 0:
            return pos - n + i + 1
        pos -= n
    return 0


def _read(f: Path, budget: int, mark) -> tuple[str, int]:
    """f's text in about `budget` bytes, and the bytes it dropped. A longer file keeps its head and its tail, cut
    at line breaks, with `mark(dropped)` between: the middle goes first and the last line (the final message) is
    always whole, so a run's end is never what a cap cuts."""
    size = f.stat().st_size
    with f.open("rb") as fh:
        if size <= budget:
            return fh.read().decode(errors="replace"), 0
        head = fh.read(int(budget * HEAD_SHARE))
        head = head[:head.rfind(b"\n") + 1]
        start = size - max(budget - len(head), 0)
        if start > 0:
            fh.seek(start - 1)
            if fh.read(1) != b"\n":   # the tail starts on a whole line
                rest = fh.read()
                cut = rest.find(b"\n")
                start = start + cut + 1 if cut >= 0 else size
        start = min(start, _last_line(fh, size))
        if start <= len(head):
            fh.seek(0)
            return fh.read().decode(errors="replace"), 0
        fh.seek(start)
        tail = fh.read()
    dropped = start - len(head)
    return head.decode(errors="replace") + mark(dropped) + tail.decode(errors="replace"), dropped


def _jsonl_mark(n: int) -> str:
    note = "the middle of this transcript was trimmed to keep the packet within its size cap"
    return json.dumps({"type": "trimmed", "bytes": n, "note": note}) + "\n"


def _text_mark(n: int) -> str:
    return f"\n[... {n} bytes trimmed here to keep the packet within its size cap ...]\n"


def packet(state: str | Path, key: str, run_dir, handoff: str | None = None) -> str:
    """A masked copy of a reviewed task's runs for a blind review, under <state>/blind/<key>/ (state: the folder of
    project.db): each run's result.json (its hand-off) and progress.md and its transcript (output.jsonl) with arm
    fields dropped and model names masked, never run.json, prompts or logs. `run_dir` is one run folder or a list
    of them, oldest first (run_dirs): one run's files sit in the packet itself; several runs' in run-<i>-of-<n>/
    folders, named only by their order, with runs.md listing them. The transcripts share PACKET_MAX bytes (as do
    the progress files): a longer one loses its middle, never its hand-off or its final messages. Returns the
    folder ("" when there is no run folder and no hand-off)."""
    dirs = [run_dir] if isinstance(run_dir, (str, Path)) or run_dir is None else list(run_dir)
    srcs = [Path(d) for d in dirs if d and Path(d).is_dir()]
    srcs = [d for d in srcs if any((d / n).is_file() for n in PACKET_FILES)] or srcs[-1:]
    if not srcs and handoff is None:
        return ""
    dst = Path(state) / "blind" / re.sub(r"[^\w.-]", "_", key)
    dst.mkdir(parents=True, exist_ok=True)
    names: dict = {}   # one key numbering for the whole packet, across its runs
    n = len(srcs)
    segs = [dst if n == 1 else dst / f"run-{i}-of-{n}" for i in range(1, n + 1)]
    budget = {}
    for name in ("progress.md", "output.jsonl"):
        sizes, last = [], []   # each file's size and the size of its last line, which it always keeps
        for f in (s / name for s in srcs):
            size = f.stat().st_size if f.is_file() else 0
            with (f.open("rb") if size else open(os.devnull, "rb")) as fh:
                last.append(size - _last_line(fh, size))
            sizes.append(size)
        rest = _shares([a - b for a, b in zip(sizes, last)], max(PACKET_MAX - sum(last), 0))
        budget[name] = [a + b for a, b in zip(last, rest)]
    trimmed = []
    for i, (src, seg) in enumerate(zip(srcs, segs)):
        for name in PACKET_FILES:
            f = src / name
            if not f.is_file():
                continue
            if name == "result.json":   # the hand-off: whole, under the per-file cap as ever
                raw = f.read_bytes()[:PACKET_MAX].decode(errors="replace")
                try:
                    text = json.dumps(mask(json.loads(raw), names=names), indent=2)
                except ValueError:
                    text = scrub(raw)
            elif name.endswith(".jsonl"):
                raw, cut = _read(f, budget[name][i], _jsonl_mark)
                text = _jsonl(raw, names)
                trimmed += [(i, name, cut)] if cut else []
            else:
                raw, cut = _read(f, budget[name][i], _text_mark)
                text = scrub(raw)
                trimmed += [(i, name, cut)] if cut else []
            if seg != dst:
                seg.mkdir(exist_ok=True)
            _write(seg / name, text)
    if n > 1:
        lines = [f"This task ran {n} times (for example a hand-off that waited and its resume, a resume after its "
                 "context was compacted, or one run per device leg). Read the runs in order: each folder holds that "
                 "run's hand-off (result.json), progress (progress.md) and transcript (output.jsonl); the last run's "
                 "hand-off is the task's final result.", ""]
        for i, seg in enumerate(segs):
            cuts = [f"{name}: {cut} bytes trimmed from its middle" for j, name, cut in trimmed if j == i]
            lines.append(f"- run {i + 1} of {n}: {seg.name}/" + (f" ({'; '.join(cuts)})" if cuts else ""))
        _write(dst / "runs.md", "\n".join(lines) + "\n")
    if handoff is not None:
        _write(dst / "plan.md", scrub(handoff))
    return str(dst)
