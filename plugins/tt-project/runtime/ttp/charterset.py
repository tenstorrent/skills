"""One-time, model-free move of the settings older harnesses read from charter prose into the
project's config (owner's plan #2119 P4): the gates that place work and pick its branch read only
structured settings, so rewording, reordering or deleting charter text changes none of them.

- `machines.allowed`: the machines the charter's Resources section names (machine_ledger.charter_machines
  over the live machine list), written only when the config authorizes none itself (`resources`,
  `shared_resources`, `machines.allowed`), since the old reader read the charter only then. It is a
  user-only key, so it never gets a machine whose machines-list record is retired or decommissioned
  (machine_conflicts, structured facts only: no charter prose, #2116): those are left out and one
  `config_conflict` event lists them with the exact `ttp config` command that would add them. A machine a
  user pause holds is kept (the pause holds its work) and named as held in that event, never in a command.
  A list already set gets the same event once (item "machine_conflicts") when the harness or an unknown
  setter wrote it, and is never changed; a list the user set is not questioned. `delivery.push_allowed`, the
  other user-only key, is never written.
- `delivery.base_ref`: the first charter branch (`branch <name>`) that exists here or on origin,
  written only when `delivery.base_ref` is unset and `delivery.push_branch` names no branch that
  exists, since the old base_ref took the charter's only then. Never `delivery.push_branch`: that
  enables pushes, which no prose ever did.
- `worktree.shared_branches`: the charter's branches other than the push and base branches, which
  no task claims as its own, kept only from a structured source: the name is backticked in the charter
  (`branch `<name>``) or exists here or on origin (a prose word after "branch", as in "the branch is
  ..." or "branch can ...", is no branch, '/' or not). A list the harness wrote before that check loses
  its prose words once (items "branch_words" and, under the backtick-or-ref rule, "branch_refs"); a list
  a user or coordinator set is never touched.
- `memory.forbidden_patterns`: the backticked regexes of the charter's "Forbidden data" section, with
  their labels, added to the config's own list (forbidden.declared reads only the config). Patterns
  only refuse writes, so moving them never widens what is allowed; forbidden.declared runs this item
  itself before its first read, so no pattern lapses before the daemon starts.

Each item runs once per project (kv DONE_KEY), written through set_config with a "harness" setter
record, so `ttp config <key> ''` reverts it and no later run fills it again from the charter. It
writes nothing the old parser did not return.
"""
from __future__ import annotations

import re

DONE_KEY = "charterset_done"   # kv: {item: epoch} for each item migrated (or found nothing to move)
ITEMS = ("machines", "branches", "branch_words", "forbidden", "branch_refs", "machine_conflicts")
CONFLICT_EVENT = "config_conflict"
USER_NEVER = frozenset({"delivery.push_allowed"})   # user-only keys the migration never writes

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


def _real_branch(p, name: str, charter: str = "") -> bool:
    """A charter word that names a branch: backticked in the charter, or a ref here or on origin (a record).
    Never a bare word, '/' or not: prose such as "and/or" has one."""
    from . import worktree as wt
    return f"`{name}`" in charter or wt._has(p, name) or wt._has(p, f"origin/{name}")


def _user_paused(p, alias: str) -> bool:
    """Whether a pause the user set (this project's or a shared one) holds machine `alias` or a resource on it."""
    try:
        rows = p.db.paused_resources()
    except Exception:
        return False
    return any(isinstance(v, dict) and v.get("by") == "user" and (k == alias or k.startswith((alias + "-", alias + ":")))
               for k, v in rows.items())


def machine_conflicts(p, names, known) -> tuple[list[str], list[str]]:
    """Of `names`, (refused, held) from structured facts alone, never charter prose (#2116): refused, the
    machines whose list entry in `known` is retired or decommissioned (machines.retired); held, the others a
    user pause holds, which is temporary and no reason to drop them."""
    from . import machines
    names = [str(n) for n in names]
    known = known if isinstance(known, dict) else {}
    refused = [a for a in names if a in known and machines.retired(known.get(a))]
    return refused, [a for a in names if a not in refused and _user_paused(p, a)]


def conflict_event(p, key: str, have: list, bad: list, wrote: bool, held: list | None = None) -> None:
    """One `config_conflict` event: the entries of user-only `key` whose machine records are retired or
    decommissioned, and the exact command that sets the list with (wrote) or without them, for the user alone
    to run. Machines a user pause holds (`held`) are named as held, never put in the command."""
    import json
    import time
    full = sorted({*map(str, have), *bad}) if wrote else list(have)
    clean = [a for a in have if a not in bad]
    if wrote:
        text = (f"Moving the charter's machine list into {key}, the harness left out {', '.join(bad)}: their machine "
                f"records are retired or decommissioned. {key} is now {json.dumps(clean)}. Only the user adds them, "
                f"if they meant to: `ttp config {p.name} {key} '{json.dumps(full)}'`.")
    else:
        text = (f"{key} lists {', '.join(bad)}, whose machine records are retired or decommissioned. The harness "
                f"changes no value of this user-only key; to drop them the user runs "
                f"`ttp config {p.name} {key} '{json.dumps(clean)}'`.")
    if held:
        text += f" A user pause holds {', '.join(held)}: that is temporary and keeps them in the list."
    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
           (time.time(), "daemon", CONFLICT_EVENT, f"{CONFLICT_EVENT}:{key}:{','.join(sorted(bad))}", "normal",
            text[:4000], "queued"))


def forbidden_patterns(charter: str) -> list[dict]:
    """{"name", "regex"} for each backticked regex of the charter's "Forbidden data" section, as
    forbidden.declared read them before (the words before the span on its line are its label)."""
    from .forbidden import _TICKS, is_section
    from .prompts import charter_sections
    out: list[dict] = []
    for heading, body in charter_sections(charter or ""):
        if not is_section(heading):
            continue
        for line in body:
            for rx in _TICKS.findall(line):
                if rx.strip():
                    label = line.split("`", 1)[0].strip().lstrip("-*").strip().rstrip(":").strip()
                    out.append({"name": label or f"charter Forbidden data item {len(out) + 1}", "regex": rx})
    return out


def forbidden_merged(p, charter: str) -> list | None:
    """memory.forbidden_patterns with the charter's patterns it lacks appended, or None (nothing to add)."""
    mem = p.raw_config().get("memory")
    have = list((mem or {}).get("forbidden_patterns") or []) if isinstance(mem, dict) else []
    rxs = {str(i.get("regex") if isinstance(i, dict) else i) for i in have}
    new = [i for i in forbidden_patterns(charter) if i["regex"] not in rxs]
    return [*have, *new] if new else None


def _branch_words(p, charter: str = "") -> list | None:
    """worktree.shared_branches without its prose words, when the harness wrote it (never a list a user
    or coordinator set), or None (nothing to drop)."""
    from . import configwho
    raw = p.raw_config()
    w = raw.get("worktree") if isinstance(raw.get("worktree"), dict) else {}
    have = w.get("shared_branches")
    if not isinstance(have, list) or configwho.role(configwho.lookup(p.db, "worktree.shared_branches", raw)) != "harness":
        return None
    keep = [n for n in have if _real_branch(p, str(n), charter)]
    return keep if keep != have else None


def _flag_set_machines(p) -> None:
    """Item "machine_conflicts": a machines.allowed the harness (or an unknown setter) wrote that lists a machine
    whose record is retired or decommissioned gets one conflict_event; the value is never changed."""
    from . import configwho
    from . import machines
    raw = p.raw_config()
    have = (raw.get("machines") or {}).get("allowed") if isinstance(raw.get("machines"), dict) else None
    if not isinstance(have, list) or not have:
        return
    if configwho.role(configwho.lookup(p.db, "machines.allowed", raw)) not in ("harness", "unknown"):
        return
    bad, held = machine_conflicts(p, have, machines.load())
    if bad:
        conflict_event(p, "machines.allowed", have, bad, wrote=False, held=held)


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
    if extra := [n for n in names if n not in plain and n not in have and _real_branch(p, n, charter)]:
        out["worktree.shared_branches"] = [*have, *extra]
    return out


def migrate(p, log=None, only: tuple[str, ...] = ITEMS) -> dict[str, object]:
    """Write plan()'s settings for the items (of `only`) not yet done and mark them done. Returns what it
    wrote. A project with no database or a config that does not read cleanly is left for a later start."""
    from . import machines
    if not p.established():
        return {}
    p.raw_config()
    if p.config_status != "ok":
        return {}
    done = p.db.kv(DONE_KEY) or {}
    todo = [i for i in only if i not in done]
    if not todo:
        return {}
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    want = [(k, v, {"machines.allowed": "machines"}.get(k, "branches"))
            for k, v in plan(p, charter, machines.load()).items()] if {"machines", "branches"} & set(todo) else []
    for item in ("branch_words", "branch_refs"):
        if item in todo and "branches" in done and (keep := _branch_words(p, charter)) is not None:
            want.append(("worktree.shared_branches", keep, item))
            break
    if "machine_conflicts" in todo and "machines" in done:
        _flag_set_machines(p)
    if "forbidden" in todo and (pats := forbidden_merged(p, charter)) is not None:
        want.append(("memory.forbidden_patterns", pats, "forbidden"))
    wrote: dict[str, object] = {}
    for key, value, item in want:
        if item in todo and key == "machines.allowed":
            bad, held = machine_conflicts(p, value, machines.load())
            if bad:
                value = [a for a in value if a not in bad]
                conflict_event(p, key, value, bad, wrote=True, held=held)
                if log:
                    log(f"charter settings: {key} left out {', '.join(bad)} (machine records retired or "
                        "decommissioned); config_conflict event raised")
            if not value:
                continue
        if item in todo and key not in USER_NEVER:
            p.set_config(key, value, by={"by": "harness", "via": "charter settings moved to config"})
            wrote[key] = value
            if log:
                log(f"charter settings: {key} set to {value} from the charter's text, once "
                    f"(`ttp config {key} ''` reverts it); the charter's wording no longer moves it")
    import time
    p.db.set_kv(DONE_KEY, {**done, **{i: int(time.time()) for i in todo}})
    return wrote
