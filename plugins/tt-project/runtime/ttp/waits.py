"""What a stuck task waits on, in plain words, for the web board, `ttp status` and the relay.

A task blocked on the user (an open ask that names it, or a `waits:user:...` label) belongs under
'Waiting on you'. Every other stuck task is the project's to move: it waits on a resource, budget,
a review, the coordinator or a time, and the user is shown that, not asked."""
from __future__ import annotations

import json
import re
import time

KINDS = ("user", "resource", "budget", "review", "coordinator", "time")
_WHO = {"user": "you", "resource": "a resource", "budget": "budget", "review": "a review",
        "coordinator": "the coordinator", "time": "a later time"}
_TEXT_CHARS = 200


def _label(task: dict) -> tuple[str, str] | None:
    """The task's `waits:<kind>:<value>` label, when it carries one with a known kind."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return None
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith("waits:"):
            _, kind, value = (lb.split(":", 2) + [""])[:3]
            if kind in KINDS:
                return kind, value.strip()
    return None


def _clip(s: str, n: int = _TEXT_CHARS) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def asks_for(task_id: int, open_asks: list[dict]) -> list[dict]:
    """The open asks whose text names the task as #<id>."""
    pat = re.compile(rf"#{int(task_id)}(?!\d)")
    return [a for a in open_asks if a.get("kind", "ask") == "ask" and pat.search(str(a.get("text") or ""))]


def _from_reason(reason: str) -> tuple[str, str]:
    """(kind, detail) from a blocked_reason the daemon writes; a worker's own words go to the
    coordinator, which decides or asks the user."""
    from .daemon import LOGGED_OUT_NOTE, NET_HELD_NOTE, PAUSED_NOTE
    r = reason.strip()
    if r.startswith(PAUSED_NOTE):
        what = r[len(PAUSED_NOTE):].split("; it starts once resumed")[0].strip()
        return "resource", f"paused resource {what}".strip()
    if r.startswith(LOGGED_OUT_NOTE):
        m = re.search(r"\(([^)]+)\)", r)
        return "user", f"a login to {m[1]}" if m else "a login"
    if r.startswith(NET_HELD_NOTE):
        return "resource", "the network: " + r[len(NET_HELD_NOTE):].strip()
    if r.startswith("its resource stayed busy"):
        return "resource", "a busy resource"
    if r.startswith("waiting for "):
        return "resource", re.sub(r";? *(its probe says .*)?$", "", r[len("waiting for "):]).strip()
    if r == "task budget exhausted":
        return "budget", "its task budget is used up"
    if re.match(r"its \$[0-9.]+ budget is above the dollar cap", r):
        return "budget", "its budget is above the dollar cap"
    if r.startswith("interrupted by"):
        return "time", "the restart to finish (it resumes)"
    if r.startswith("workspace:"):
        return "coordinator", "a workspace fix: " + r[len("workspace:"):].strip()
    # A dead dependency, a host-reboot loop, a refused push, a worker's question: the coordinator's.
    return "coordinator", r


def wait_kind(task: dict, open_asks: list[dict], now: float | None = None) -> dict:
    """What `task` waits on: {"kind": one of KINDS, "text": "waits on ...", "since": epoch,
    "age": "3.2h", "asks": [ask ids]}. The `waits:` label wins, then open asks that name the
    task, then the blocked_reason's prefix."""
    now = time.time() if now is None else now
    since = float(task.get("updated") or task.get("created") or now)
    linked = asks_for(task["id"], open_asks)
    lab = _label(task)
    if lab:
        kind, detail = lab
    elif linked:
        kind, detail = "user", ", ".join(f"ask {a['id']}" for a in linked)
        since = min(float(a.get("ts") or since) for a in linked)
    elif task.get("status") == "review":
        kind, detail = "review", ""
    else:
        kind, detail = _from_reason(task.get("blocked_reason") or "")
    if kind == "time" and re.fullmatch(r"[0-9]+(\.[0-9]+)?", detail):
        text = "waits until " + time.strftime("%Y-%m-%d %H:%M", time.localtime(float(detail)))
    elif kind in ("resource", "time") and detail:
        text = f"waits on {_clip(detail)}"
    else:
        text = f"waits on {_WHO[kind]}" + (f": {_clip(detail)}" if detail else "")
    age_s = max(0.0, now - since)
    return {"kind": kind, "text": text, "since": since, "age": f"{age_s / 3600:.1f}h",
            "asks": [a["id"] for a in linked]}


def split(tasks: list[dict], open_asks: list[dict], now: float | None = None) -> tuple[list[dict], list[dict]]:
    """Blocked tasks as (waiting on the user, stuck with the project on it), each task with its
    `wait` (see wait_kind)."""
    you, stuck = [], []
    for t in tasks:
        if t.get("status") != "blocked":
            continue
        w = wait_kind(t, open_asks, now)
        (you if w["kind"] == "user" else stuck).append({**t, "wait": w})
    return you, stuck


def open_asks(db) -> list[dict]:
    """The asks still open, to link to the tasks they name."""
    return db.q("SELECT id, ts, kind, text FROM messages WHERE direction='out' AND kind='ask' AND handled=0 "
                "ORDER BY id DESC")
