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

BLIND_LABEL = "blind_review"
# Labels of the reviews the daemon queues for device work (deviceab and deviceshadow: first and second
# reviewer). Each names its reviewer slot and subject, so two of them differ when their labels do, whatever
# their titles say (supersede.duplicate_of), and they are not the coordinator's new tasks (coordinator.tasks_made).
DAEMON_REVIEW_PREFIXES = ("device_ab_review", "device_shadow_review")
ENV = "TTP_BLIND"
CODES_KV = "blind_review_codes"   # {code: "<tid>:<arm>"}

# What a blind caller may run: hand-off, notes and reads of its own task; nothing that reports runs, models,
# spend or stats.
ALLOWED = {"cmd_note", "cmd_progress", "cmd_notify", "cmd_upstream", "cmd_task", "cmd_detach", "cmd_lock",
           "cmd_clip", "cmd_hours", "cmd_alive", "cmd_landed"}
TASK_ACTIONS = {"show", "list"}

# Fields that name or hint at the arm: the model, provider and effort pins, the account, and spend (an open
# model on a free plan shows as $0).
MASK_KEYS = {"model", "provider", "effort", "pin_verify", "account", "spent_usd", "budget_usd", "usd", "cost_usd",
             "called", "models", "providers", "arm", "arms"}
ARM_LABELS = ("device_ab", "device_shadow", "model:", "provider:", "effort:", "pin:", "route:", "jev")

# Model ids and family words. Word-bounded and case-insensitive; a path such as `.claude/` is left alone (the
# lookarounds refuse a word next to `.`, `/` or `-` path parts) so masked text stays readable.
_FAMILIES = (r"claude(?:-[a-z0-9.]+)*|opus|sonnet|haiku|fable|glm[-a-z0-9.]*|gpt-?[a-z0-9.]*|"
             r"gemini[-a-z0-9.]*|qwen[-a-z0-9.]*|deepseek[-a-z0-9.]*|gemma[-a-z0-9.]*|llama[-a-z0-9.]*|"
             r"mistral[-a-z0-9.]*|kimi[-a-z0-9.]*|minimax[-a-z0-9.]*|codex|cursor|anthropic|openai|zhipu|"
             r"chatglm|z\.ai")
WORDS = re.compile(rf"(?<![\w./-])(?:{_FAMILIES})(?![\w/])", re.I)
MASK = "[model]"
PACKET_MAX = 2_000_000   # bytes per file in a packet

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


def _labels(raw) -> list[str]:
    try:
        return [str(x) for x in (json.loads(raw or "[]") if isinstance(raw, str) else raw or [])]
    except ValueError:
        return []


def review_slots(task: dict | None) -> set[str]:
    """The daemon review labels `task` carries (its reviewer slot and subject); empty for any other task."""
    return {lb for lb in _labels((task or {}).get("labels")) if lb.startswith(DAEMON_REVIEW_PREFIXES)}


def is_blind_task(task: dict | None) -> bool:
    return bool(task) and BLIND_LABEL in _labels(task.get("labels"))


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


def packet(state: str | Path, key: str, run_dir: str | Path | None, handoff: str | None = None) -> str:
    """A masked copy of a reviewed run for a blind review, under <state>/blind/<key>/ (state: the folder of project.db): its result.json
    and progress.md and its transcript (output.jsonl) with arm fields dropped and model names masked, never
    run.json, prompts or logs. Returns the folder ("" when there is no run folder and no hand-off)."""
    src = Path(run_dir) if run_dir else None
    if not (src and src.is_dir()) and handoff is None:
        return ""
    dst = Path(state) / "blind" / re.sub(r"[^\w.-]", "_", key)
    dst.mkdir(parents=True, exist_ok=True)
    names: dict = {}   # one key numbering for the whole packet
    for name in ("result.json", "progress.md", "output.jsonl"):
        f = src / name if src else None
        if not (f and f.is_file()):
            continue
        raw = f.read_bytes()[:PACKET_MAX].decode(errors="replace")
        if name == "result.json":
            try:
                text = json.dumps(mask(json.loads(raw), names=names), indent=2)
            except ValueError:
                text = scrub(raw)
        elif name.endswith(".jsonl"):
            text = _jsonl(raw, names)
        else:
            text = scrub(raw)
        _write(dst / name, text)
    if handoff is not None:
        _write(dst / "plan.md", scrub(handoff))
    return str(dst)
