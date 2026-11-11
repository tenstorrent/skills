"""One-time, model-free move of the settings older harnesses read from charter prose into the
project's config (owner's plan #2119 P4): the gates that place work and pick its branch read only
structured settings, so rewording, reordering or deleting charter text changes none of them.

- `machines.allowed`: the machines the charter's Resources section names (machine_ledger.charter_machines
  over the live machine list), written only when the config authorizes none itself (`resources`,
  `shared_resources`, `machines.allowed`), since the old reader read the charter only then.
- `delivery.base_ref`: the first charter branch (`branch <name>`) that exists here or on origin,
  written only when `delivery.base_ref` is unset and `delivery.push_branch` names no branch that
  exists, since the old base_ref took the charter's only then. Never `delivery.push_branch`: that
  enables pushes, which no prose ever did.
- `worktree.shared_branches`: the charter's branches other than the push and base branches, which
  no task claims as its own.

Each item runs once per project (kv DONE_KEY), written through set_config with a "harness" setter
record, so `ttp config <key> ''` reverts it and no later run fills it again from the charter. It
writes nothing the old parser did not return.
"""
from __future__ import annotations

import re

DONE_KEY = "charterset_done"   # kv: {item: epoch} for each item migrated (or found nothing to move)
ITEMS = ("machines", "branches")

# "branch <name>", "branch: <name>" or "branch `<name>`" in the charter (the old worktree reader).
CHARTER_BRANCH = re.compile(r"\bbranch\b[:\s]+`?([A-Za-z0-9][\w./-]*[\w-])`?")


def charter_branches(text: str) -> list[str]:
    """Branch names the charter gives as `branch <name>`, in order, without main, master and HEAD (a
    charter says those to forbid them)."""
    out: list[str] = []
    for name in CHARTER_BRANCH.findall(text):
        if name not in ("HEAD", "main", "master") and name not in out:
            out.append(name)
    return out


def plan(p, charter: str, known: dict) -> dict[str, object]:
    """{config key: value} the migration would write for `p` (empty: nothing to move). Reads git
    only to tell which charter branch exists, as the old base_ref did."""
    from . import machine_ledger as ml
    from . import worktree as wt
    raw = p.raw_config()
    out: dict[str, object] = {}
    live = ml.live_machines(known)
    if not ml.configured_machines(raw, live) and (names := ml.charter_machines(charter, live)):
        out["machines.allowed"] = names
    names = charter_branches(charter)
    d = raw.get("delivery") if isinstance(raw.get("delivery"), dict) else {}
    base, push = str(d.get("base_ref") or "").strip(), str(d.get("push_branch") or "").strip()
    if names and not base and not any(wt._has(p, c) for c in ((f"origin/{push}", push) if push else ())):
        for ref in names:
            if cand := next((c for c in (f"origin/{ref}", ref) if wt._has(p, c)), ""):
                out["delivery.base_ref"] = base = cand
                break
    plain = {base, base[7:] if base.startswith("origin/") else base, push}
    w = raw.get("worktree") if isinstance(raw.get("worktree"), dict) else {}
    have = w.get("shared_branches") if isinstance(w.get("shared_branches"), list) else []
    if extra := [n for n in names if n not in plain and n not in have]:
        out["worktree.shared_branches"] = [*have, *extra]
    return out


def migrate(p, log=None) -> dict[str, object]:
    """Write plan()'s settings for the items not yet done and mark them done. Returns what it wrote.
    A project with no database or a config that does not read cleanly is left for a later start."""
    from . import machines
    if not p.established():
        return {}
    p.raw_config()
    if p.config_status != "ok":
        return {}
    done = p.db.kv(DONE_KEY) or {}
    todo = [i for i in ITEMS if i not in done]
    if not todo:
        return {}
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    want = plan(p, charter, machines.load())
    item = {"machines.allowed": "machines", "delivery.base_ref": "branches", "worktree.shared_branches": "branches"}
    wrote: dict[str, object] = {}
    for key, value in want.items():
        if item[key] in todo:
            p.set_config(key, value, by={"by": "harness", "via": "charter settings moved to config"})
            wrote[key] = value
            if log:
                log(f"charter settings: {key} set to {value} from the charter's text, once "
                    f"(`ttp config {key} ''` reverts it); the charter's wording no longer moves it")
    import time
    p.db.set_kv(DONE_KEY, {**done, **{i: int(time.time()) for i in todo}})
    return wrote
