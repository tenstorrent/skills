"""Missing entries: a guard or allowlist refused a step the user ordered.

A worker whose step the user ordered is refused by a guard or allowlist notes it in one structured line,
`ttp note "missing-entry: <setting or file> | <entry> | order #<id>"`, and waits in its run to retry.
parse() reads the line and raise_() turns it into one must-act event (KIND), which wakes the coordinator
at once: it adds the entry in the same turn and wakes or requeues the task, unless a Restriction, a user
hold or a user-only setting (machines.allowed, push permission) forbids it. Model-free."""

from __future__ import annotations

import re
import time

KIND = "missing_entry"
LINE = re.compile(r"^\s*missing-entry:\s*(?P<rule>[^|\n]+?)\s*\|\s*(?P<entry>[^|\n]+?)\s*\|\s*"
                  r"(?:order\s*)?(?:user\s+msg\s*)?#?(?P<order>\d+)", re.I | re.M)


def parse(text: str) -> dict | None:
    """{"rule", "entry", "order"} of the first missing-entry line in `text`, else None."""
    m = LINE.search(str(text or ""))
    if not m:
        return None
    return {"rule": m["rule"].strip("` "), "entry": m["entry"].strip("` "), "order": int(m["order"])}


def key(tid: int) -> str:
    return f"missing-entry-{int(tid)}"


def raise_(db, tid: int, got: dict, now: float | None = None) -> bool:
    """One must-act event for task `tid`'s missing entry `got` (parse), once per rule and entry."""
    from . import obligations
    now = time.time() if now is None else now
    m = db.one("SELECT chat FROM messages WHERE id=?", (got["order"],))
    chat = obligations._chat(m["chat"] if m else None)
    text = (f"Task #{tid}: a guard or allowlist refused a step the user ordered (user msg #{got['order']}). "
            f"Missing entry: `{got['entry']}` in {got['rule']}. Add it now, then wake or requeue #{tid}, "
            f"unless a Restriction, a user hold or a user-only setting forbids it (then a noop naming the key "
            f"and that rule).")
    data = {"must_act": {"key": key(tid), "chat": chat, "tasks": [int(tid)], "kind": KIND, **got}}
    return obligations._raise(db, now, f"{KIND}:{tid}:{got['rule']}|{got['entry']}", KIND, text, int(tid), data)


def from_note(p, tid: int, text: str) -> bool:
    """Raise the must-act event a worker's note asks for; False when the note has no missing-entry line."""
    got = parse(text)
    return bool(got) and raise_(p.db, tid, got)
