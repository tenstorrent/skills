"""What a stuck task waits on, in plain words, for the web board, `ttp status` and the relay.

A task blocked on the user (an open ask that names it, a `waits:user:...` label, a coordinator hold
anchored on an ask still open, `waits:ask:<id>`, or a resource the user paused) belongs under 'Waiting on you'.
Every other stuck task is the project's to move: it waits on a resource, budget, a review, the coordinator or a
time, and the user is shown that, not asked. An unknown reason is the coordinator's, never the user's.
`ttp status`, `ttp overview`, the web app and the relay all count through `split`, so they cannot disagree."""
from __future__ import annotations

import json
import re
import time

from . import timefmt

KINDS = ("user", "resource", "budget", "review", "coordinator", "time")
_WHO = {"user": "you", "resource": "a resource", "budget": "budget", "review": "a review",
        "coordinator": "the coordinator", "time": "a later time"}
_TEXT_CHARS = 200
# A coordinator hold's anchor (anchors.py) as the kind it waits on: an ask is the user's, an until a time,
# a when probe or a resource the project's to watch.
_ANCHOR_KINDS = {"ask": "user", "until": "time", "when": "resource", "resource": "resource"}


def _when_words(probe: str) -> str:
    """A `when:` hold in plain words, as starts_text says a start_when: never the probe itself."""
    landed = re.fullmatch(r"landed:#([0-9]+)", probe.strip())
    return f"#{landed[1]} to land" if landed else "a check to pass"


def _label(task: dict) -> tuple[str, str] | None:
    """The task's `waits:<kind>:<value>` label, when it carries one with a known kind, a hold's anchor
    mapped to its kind: (user, 'ask <id>'), (time, epoch), (resource, plain words or the name)."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return None
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith("waits:"):
            _, kind, value = (lb.split(":", 2) + [""])[:3]
            if kind in KINDS:
                return kind, value.strip()
            if kind in _ANCHOR_KINDS and value.strip():
                value = value.strip()
                if kind == "ask":
                    value = "an ask" if value == "new" else f"ask {value.lstrip('#')}"
                elif kind == "when":
                    value = _when_words(value)
                return _ANCHOR_KINDS[kind], value
    return None


def _held_ask(task: dict) -> int | None:
    """The id of the ask a hold is anchored on (`waits:ask:<id>`), or None."""
    from .anchors import anchor
    got = anchor(task)
    return int(got[1].lstrip("#")) if got and got[0] == "ask" and got[1].lstrip("#").isdigit() else None


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
    from .daemon import LOGGED_OUT_NOTE, NET_HELD_NOTE, PAUSED_NOTE, BRANCH_HELD_NOTE, WORKSPACE_RETRY_NOTE
    r = reason.strip()
    if r.startswith(BRANCH_HELD_NOTE):
        return "time", "#" + r[len(BRANCH_HELD_NOTE):].split("; it starts once")[0].strip() + " to end"
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
    if r.startswith(WORKSPACE_RETRY_NOTE):
        return "time", "a retry of its workspace (it retries by itself)"
    if r.startswith("workspace:"):
        return "coordinator", "a workspace fix: " + r[len("workspace:"):].strip()
    # A dead dependency, a host-reboot loop, a refused push, a worker's question: the coordinator's.
    return "coordinator", r


def _user_paused(detail: str, paused: dict | None) -> str:
    """The resource named by a resource wait's detail (a name, or 'paused resource <name>: <why>')
    when the user paused it, else ''. A pause the coordinator or a project set is not the user's."""
    for name in {detail.strip(), detail.removeprefix("paused resource ").split(":")[0].strip()}:
        if name and ((paused or {}).get(name) or {}).get("by") == "user":
            return name
    return ""


def wait_kind(task: dict, open_asks: list[dict], now: float | None = None, where=None,
              paused: dict | None = None, entered: float | None = None) -> dict:
    """What `task` waits on: {"kind": one of KINDS, "text": "waits on ...", "since": epoch or None,
    "age": "3.2h" or "unknown", "updated": epoch or None, "age_text": "waiting 3.2h, last update 0.5h ago",
    "asks": [ask ids]}. The `waits:` label wins, then open asks that name the task, then the
    blocked_reason's prefix. A hold on an ask that is no longer open waits on the coordinator; a resource
    wait is the user's only when `paused` (DB.paused_resources) says the user paused it. A time is shown
    in the home zone `where` (UTC without one).
    `since` is when the wait began: the oldest open ask it waits on, else `entered` (sweep.entered, when
    the stuck episode began), else unknown. The last update (a new reason or hold) is `updated`, apart:
    it never stands in for the start."""
    from .anchors import anchor
    now = time.time() if now is None else now
    since = float(entered) if isinstance(entered, (int, float)) else None
    linked = asks_for(task["id"], open_asks)
    lab = _label(task)
    held = _held_ask(task)
    if held is not None:   # the ask a hold is anchored on counts as the task's, named or not
        linked += [a for a in open_asks if a.get("id") == held and a not in linked]
    if lab:
        kind, detail = lab
        on_ask = (anchor(task) or ("",))[0] == "ask"
        if on_ask and not any(a.get("id") == held for a in linked):
            # The ask was answered or withdrawn (or never sent): acting on it is the coordinator's.
            kind, detail = "coordinator", (f"the answer to ask {held}" if held is not None else "an ask not sent")
        elif held is not None and linked:
            since = _asked(linked, since)
    elif linked:
        kind, detail = "user", ", ".join(f"ask {a['id']}" for a in linked)
        since = _asked(linked, since)
    elif task.get("status") == "review":
        kind, detail = "review", ""
    else:
        kind, detail = _from_reason(task.get("blocked_reason") or "")
    if kind == "resource" and (name := _user_paused(detail, paused)):
        kind, detail = "user", f"{name}, which you paused"
    if kind == "time" and re.fullmatch(r"[0-9]+(\.[0-9]+)?", detail):
        text = "waits until " + timefmt.long(float(detail), where)
    elif kind in ("resource", "time") and detail:
        text = f"waits on {_clip(detail)}"
    elif kind == "user" and (mine := [a for a in linked if a.get("text")]):
        from .pendingask import full   # the ask in full, never clipped to its id (user msg #2424)
        text = f"waits on {_WHO[kind]}: " + "; ".join(full(a) for a in mine)
    else:
        text = f"waits on {_WHO[kind]}" + (f": {_clip(detail)}" if detail else "")
    age = "unknown" if since is None else _hours(now - since)
    upd = float(task["updated"]) if isinstance(task.get("updated"), (int, float)) else None
    age_text = f"waiting {age}" if since is not None else "waiting since an unknown time"
    if upd is not None and (since is None or upd - since >= 60):
        age_text += f", last update {_hours(now - upd)} ago"
    return {"kind": kind, "text": text, "since": since, "age": age, "updated": upd, "age_text": age_text,
            "asks": [a["id"] for a in linked]}


def _hours(s: float) -> str:
    return f"{max(0.0, s) / 3600:.1f}h"


def _asked(linked: list[dict], since: float | None) -> float | None:
    """When the oldest of these asks was sent, else `since`."""
    sent = [float(a["ts"]) for a in linked if a.get("ts")]
    return min(sent) if sent else since


def split(tasks: list[dict], open_asks: list[dict], now: float | None = None, where=None,
          paused: dict | None = None, entered: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Blocked tasks as (waiting on the user, stuck with the project on it), each task with its
    `wait` (see wait_kind); `entered` is {task id: when its stuck episode began} (unknown if absent)."""
    you, stuck = [], []
    for t in tasks:
        if t.get("status") != "blocked":
            continue
        w = wait_kind(t, open_asks, now, where, paused, (entered or {}).get(t["id"]))
        (you if w["kind"] == "user" else stuck).append({**t, "wait": w})
    return you, stuck


def blocked(db, now: float | None = None, where=None, ages: bool = True) -> tuple[list[dict], list[dict]]:
    """`split` of the project's blocked tasks against its open asks and resource pauses: the one
    count of what waits on the user, for `ttp status`, the web app and `ttp overview` (which only
    counts: `ages` False leaves each wait's start unknown)."""
    from .sweep import entered_map
    tasks = db.q("SELECT * FROM tasks WHERE status='blocked' ORDER BY priority, id")
    now = time.time() if now is None else now
    return split(tasks, open_asks(db), now, where, db.paused_resources(),
                 entered_map(db, tasks, now) if ages else None)


def gated_on_ask(db, asks: list[dict] | None = None) -> list[dict]:
    """Queued tasks deferred on a start gate (`start_when`) that an open ask names as #<id>: only the
    user's answer can make the gate pass, so they wait on the user too."""
    from .db import deferral
    asks = open_asks(db) if asks is None else asks
    if not asks:
        return []
    return [t for t in db.q("SELECT * FROM tasks WHERE status='queued' ORDER BY priority, id")
            if deferral(t).get("when") and asks_for(t["id"], asks)]


def on_user(db, now: float | None = None, where=None, ages: bool = True) -> dict:
    """The one 'waiting on you' figure for `ttp status`, `ttp overview` and the web app: blocked tasks
    that wait on the user + open asks + tasks gated on an open ask. {"tasks", "asks", "gated", "held", "count"}."""
    you, held = blocked(db, now, where, ages)
    asks = open_asks(db)
    gated = gated_on_ask(db, asks)
    return {"tasks": you, "asks": asks, "gated": gated, "held": held, "count": len(you) + len(asks) + len(gated)}


def held_line(stuck: list[dict]) -> str:
    """The blocked tasks that do not wait on the user, by what holds them, in neutral words:
    'held: 3 blocked task(s) (waiting on the coordinator 2, a resource 1)'; '' for none."""
    if not stuck:
        return ""
    n: dict[str, int] = {}
    for t in stuck:
        n[t["wait"]["kind"]] = n.get(t["wait"]["kind"], 0) + 1
    parts = ", ".join(f"{_WHO[k]} {c}" for k, c in sorted(n.items(), key=lambda kc: (-kc[1], KINDS.index(kc[0]))))
    return f"held: {len(stuck)} blocked task(s) (waiting on {parts})"


def open_asks(db) -> list[dict]:
    """The asks still open, to link to the tasks they name."""
    return db.q("SELECT id, ts, kind, text FROM messages WHERE direction='out' AND kind='ask' AND handled=0 "
                "ORDER BY id DESC")
