# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Per-tier provider routing: one tier's worker runs go to the `chat` provider (an OpenAI-compatible
endpoint, chatbridge.py), with fallback to the core provider and a meter on every run.

Off unless project.json sets `providers.chat.tiers.<tier>.route: true` next to that tier's `model`,
and off whenever the project's providers_allowed leaves chat out (provallow.py).
A worker run of a routed tier starts on chat when its task pins no provider or model and the start is
no tier-up retry; reviews and coordinator turns never route. The run's note gets a `route` entry:
the tier, where it was routed (`to`, `model`), its `fallback` reason (none, down, error, rate_limit,
auth, malformed, timeout, failed, no_handoff, busy or rerun, with a short `cause` for a run's own end),
whether its spend is `priced`, its wall time and, when the model has effort_fields (chatbridge.py), the
`effort` it ran at with the fields sent and whether the endpoint `rejected` them. An endpoint fault also
keeps the failed answer's `upstream` HTTP status and error code (404 model_not_found apart from a 5xx;
chatbridge.upstream_of: never its text, a key, a URL or a host). The run's hand-off summary, which the
coordinator feed shows, is then one route line (fallback_line) naming the reason, never the run's error
text: the core provider's run that follows is the outcome that counts.

Effort: a routed run runs at its tier's `providers.chat.tiers.<tier>.effort`, and every request carries
the fields `providers.chat.effort_fields` gives its model at that effort; config_problems names a
routed tier whose requests would carry none. Time limit: `providers.chat.tiers.<tier>.route_timeout_s`
replaces the tier's budget.run_timeout_s for its routed runs (timeout_s); a fallback start on the core
provider keeps the tier default. Within it, the tier's budget.stall_s ends a chat run that goes silent or
spends most of its last stall_s in wait calls (runner.is_wait: long sleeps, polling loops), and it falls
back like a timeout.

Slots: a routed start takes a worker slot under the routed-to provider's gate (its max_parallel,
allow_new_work, pause and net hold), never the core provider's, and one of the host's slots for that
provider (below). When its gate holds new work or a pause holds the provider, the run starts on the
core provider, under the core gate, with fallback `busy` (no fault: no cool-down). When only slots are
full (the project's or the host's), the task waits for one up to `providers.chat.route_wait_s` (WAIT_S;
0: no wait), as a task waits for a worker slot; a run that got one after waiting says `waited_s`, and
past the wait it starts on the core provider with fallback `busy_wait_expired` and the `waited_s`.

Host slots: every project of this account on this host shares one limit per routed-to provider, so
projects using one account key never exceed it together: the account setting `route.host_max`
(`ttp config --account route.host_max N`) or TTP_ROUTE_HOST_MAX, else HOST_MAX; 0 turns it off. Each
routed start holds a lease in ~/.tt-project/route-slots/<provider>.json (host_take, under a file lock)
naming the run's supervisor pid and start, freed when the run ends (host_release); a lease whose
process is gone (a crash, a reboot) or past its run's time limit is dropped by the next reader.

Fallback: a routed run that ended on an endpoint fault (unreachable or timed out: down; 5xx or a
404: error; a 404 for a model the endpoint does not serve, or the run's own unrecognized_model error: model_missing; 429 or rate-limit wording: rate_limit; a refused key: auth, through the auth
gate; an answer that is not JSON: malformed) ends as `fallback` (or `limit`/`auth`), costs no attempt
and requeues at the same tier; the endpoint then cools down for `providers.chat.route_cooldown_s`
(COOLDOWN_S), during which the tier's runs start on the core provider. A routed run that ended without
a hand-off on its own (own_cause: timed out or stalled: timeout; a failed exit or a refused request:
failed; a clean exit without a valid result.json: no_handoff) falls back the same way, with no
cool-down: the endpoint is fine, the run was not. Its task's later runs start on the core provider
(`rerun`), so a task falls back on its own at most once. A run that handed off (even `failed`) ended
on its merits: no fallback; a failed light try is retried a tier up as usual.

Missing model: a model_missing fallback cools nothing; it holds that tier's route alone (MISSING kv,
hold), so its runs start on the core provider with fallback `model_missing` and one normal notice
names the provider, tier and model. The hold ends (release, each daemon tick) once the tier's
configured model changes or the daemon's model-free /models check (at most hourly while a tier routes:
MODELS_EVERY_S) lists the model again. That check writes the served ids, its time and HTTP status to
the project state dir's MODELS_FILE, never the key or URL; `ttp status` and doctor show holds and ids
(model_lines), so a worker can pick a replacement without the key.

Salvage: a routed run on the endpoint that exited cleanly without a result.json but printed its
hand-off in its final reply keeps it (salvage): exactly one top-level JSON object in that reply,
fenced (```json) or bare, that passes the check a written result.json gets (a JSON object whose
status is a hand-off state) is written as result.json, and the run's route entry says `salvaged`.
No object, several, or one that does not parse or pass: no salvage, the run falls back as above.
Tool output and earlier turns are never read, no field is filled in, and a run that wrote its own
result.json (valid or not) is left as it is.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

TO = "chat"                 # the provider a tier can be routed to
KEY = "route"               # the run note's entry
COOL = "route_cooldown:"    # kv: {"until", "reason", "run"} per routed-to provider
REJECTED = "route_effort_rejected:"   # kv: <model>|<effort> whose effort_fields an endpoint rejected (logged once)
COOLDOWN_S = 900
MISSING = "route_model_missing:"   # kv per routed tier: {"model", "since", "run"} while its model is not served
MODELS_FILE = "endpoint-models.json"   # under the project state dir: {provider: {"ids", "checked", "ok", "check"}}
MODELS_EVERY_S = 3600       # the daemon's model-free /models check runs at most this often per provider
RETRY_QUIET_S = 86400       # a re-hold of the same model this soon after its quiet retry sends no new notice
FAULTS = ("down", "error", "rate_limit", "auth", "malformed", "model_missing")
RUN_CAUSES = ("timeout", "failed", "no_handoff")   # a routed run's own end without a hand-off (own_cause)
OWN_CAUSE_WORDS = ("timed out", "stalled", "exited without", "result.json", "exit ")   # fallback_line shows these
# busy: the routed-to provider's gate or a pause held new work, no fault; busy_wait_expired: its slots
# (the project's or the host's) stayed full for the whole wait; rerun: the task fell back on its own before.
REASONS = ("none", *FAULTS, *RUN_CAUSES, "busy", "busy_wait_expired", "rerun")
WAIT_S = 90.0               # providers.chat.route_wait_s: how long a routed start waits for a slot
HOST_MAX = 6                # route.host_max unset: the most routed runs seen at once on one key without a 429
HOST_ENV = "TTP_ROUTE_HOST_MAX"
HOST_DIR = "route-slots"    # under ~/.tt-project: <provider>.json (the leases) and <provider>.lock
LEASE_GRACE_S = 900         # a lease outlives its run's time limit by this much at most
# Run ends a bridge fault turns into a fallback; any other end is the run's own.
FAULT_ENDS = ("failed", "request_error", "timeout", "stalled")


def target(cfg: dict, tier: str) -> tuple[str, str] | None:
    """(provider, model) the tier's worker runs are routed to, or None (unset: nothing changes)."""
    from .provallow import allows
    if (cfg.get("core_provider") or "claude") == TO or not allows(cfg, TO):
        return None
    entry = ((cfg.get("providers") or {}).get(TO) or {})
    t = ((entry.get("tiers") or {}).get(tier) or {}) if isinstance(entry, dict) else {}
    if not isinstance(t, dict) or t.get("route") is not True or not str(t.get("model") or ""):
        return None
    return TO, str(t["model"])


def timeout_s(cfg: dict, tier: str) -> float | None:
    """The tier's routed run time limit (providers.chat.tiers.<tier>.route_timeout_s), or None: the
    tier's budget.run_timeout_s applies."""
    t = (((cfg.get("providers") or {}).get(TO) or {}).get("tiers") or {}).get(tier) or {}
    v = t.get("route_timeout_s") if isinstance(t, dict) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def eligible(task: dict, picked: dict | None) -> bool:
    """A worker run that may be routed: no review, no provider or model pin, no tier-up retry."""
    return (task.get("kind") != "review" and not str(task.get("provider") or "")
            and not str(task.get("model") or "") and (picked or {}).get("by") != "retry")


def cooldown_s(cfg: dict) -> float:
    v = ((cfg.get("providers") or {}).get(TO) or {}).get("route_cooldown_s")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else COOLDOWN_S


def cooling(db, prov: str, now: float) -> str:
    """The fallback reason while `prov` cools down after one, else ''."""
    c = db.kv(COOL + prov) or {}
    return str(c.get("reason") or "error") if float(c.get("until") or 0) > now else ""


def cool(db, cfg: dict, prov: str, reason: str, run_id: int | None, now: float) -> None:
    db.set_kv(COOL + prov, {"until": now + cooldown_s(cfg), "reason": reason, "run": run_id})


def held(db, cfg: dict, tier: str) -> str:
    """The model a routed tier's hold is for while that model is still the configured one, else ''
    (a retry record, release's quiet hourly try, holds nothing)."""
    h = db.kv(MISSING + tier) or {}
    to = target(cfg, tier)
    return str(h.get("model") or "") if h and not h.get("retry") and to and to[1] == h.get("model") else ""


def hold(db, tier: str, model: str, run_id: int | None, now: float) -> bool:
    """Hold the tier's route: its endpoint does not serve `model`. True when the hold is new (the user
    is told); a re-hold of the same model within RETRY_QUIET_S of its quiet retry (release) is not."""
    h = db.kv(MISSING + tier) or {}
    if h.get("model") == model and not h.get("retry"):
        return False
    quiet = h.get("model") == model and now - float(h.get("at") or 0) < RETRY_QUIET_S
    db.set_kv(MISSING + tier, {"model": model, "since": now, "run": run_id})
    return not quiet


def holds(db) -> dict[str, dict]:
    """Every tier's model_missing hold record, by tier (a stale one included: release drops those)."""
    rows = db.q("SELECT key FROM kv WHERE substr(key, 1, ?) = ?", (len(MISSING), MISSING))
    return {r["key"][len(MISSING):]: h for r in rows if isinstance(h := db.kv(r["key"]), dict)}


def release(db, cfg: dict, served: dict, now: float) -> list[tuple[str, str, str]]:
    """End each hold whose tier's configured model changed (or is no longer routed), or once a /models
    check (`served`, read_served) after the hold began left the model out and a later one lists it
    again: [(tier, model, why)]. A check after the hold that still lists the refused model contradicts
    nothing it proves: the tier tries one routed run again (a quiet retry record) without a new notice
    for RETRY_QUIET_S, so a model the endpoint lists but refuses costs one run an hour and one notice."""
    out = []
    for tier, h in holds(db).items():
        model = str(h.get("model") or "")
        to = target(cfg, tier)
        rec = (served or {}).get(to[0] if to else TO) or {}
        fresh = rec.get("ok") and float(rec.get("checked") or 0) > float(h.get("since") or 0)
        if not to or to[1] != model:
            why = f"the configured model is now {to[1]}" if to else "the tier is no longer routed"
        elif h.get("retry"):
            if now - float(h.get("at") or 0) < RETRY_QUIET_S:
                continue
            why = "its quiet retry ended"
        elif fresh and model not in (rec.get("ids") or []):
            if not h.get("gone"):
                db.set_kv(MISSING + tier, {**h, "gone": rec.get("checked")})
            continue
        elif fresh and h.get("gone") and float(rec.get("checked") or 0) > float(h["gone"]):
            why = "the endpoint lists it again"
        elif fresh and not h.get("gone"):
            db.set_kv(MISSING + tier, {"model": model, "retry": True, "at": now})
            out.append((tier, model, "the endpoint still lists it; one routed run tries it again"))
            continue
        else:
            continue
        db.x("DELETE FROM kv WHERE key=?", (MISSING + tier,))
        out.append((tier, model, why))
    return out


def read_served(state: Path) -> dict:
    """The last model-free /models check per provider ({"ids", "checked", "ok", "check"}), or {}."""
    try:
        v = json.loads((Path(state) / MODELS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def write_served(state: Path, prov: str, ok: bool, check: str, ids: list[str] | None, now: float) -> None:
    """Record one /models check: ids only, the check time and its HTTP status line, never the key or URL.
    A failed check keeps the last ids it had (marked by `ok` false)."""
    data = read_served(state)
    last = data.get(prov) if isinstance(data.get(prov), dict) else {}
    data[prov] = {"ids": list(ids or []) if ok else list(last.get("ids") or []), "checked": now, "ok": bool(ok),
                  "check": check, **({"listed": last.get("checked")} if not ok and last.get("checked") else {})}
    from .project import durable_write
    durable_write(Path(state) / MODELS_FILE, json.dumps(data, indent=1))


def refresh_served(state: Path, alias: str) -> bool:
    """Run one /models check with the key saved for `alias` and record it (write_served); False when no
    key is saved (the auth gate tells the user). Nothing it writes holds the key or the URL."""
    from . import endpoint
    got = endpoint.credentials(alias)
    if got is None:
        return False
    ok, msg, ids = endpoint.list_models(got.url, got.key)
    write_served(state, TO, ok, msg, ids, time.time())
    return True


def models_due(cfg: dict, state: Path, now: float) -> str:
    """The endpoint alias whose /models check is due (a tier routes to it and the last check is
    MODELS_EVERY_S old), else ''."""
    entry = (cfg.get("providers") or {}).get(TO) or {}
    alias = str(entry.get("endpoint") or "") if isinstance(entry, dict) else ""
    if not alias or not any(target(cfg, t) for t in (entry.get("tiers") or {})):
        return ""
    last = read_served(state).get(TO) or {}
    return alias if now - float(last.get("checked") or 0) >= MODELS_EVERY_S else ""


def model_lines(db, cfg: dict, state: Path, now: float, at) -> list[str]:
    """`ttp status` and doctor lines: each tier held for a missing model, then what the endpoint served
    at its last /models check (`at(ts)`: a time in the home zone)."""
    rec = read_served(state).get(TO) or {}
    out = []
    for tier, h in sorted(holds(db).items()):
        if held(db, cfg, tier):
            out.append(f"routing: {tier} held since {at(float(h.get('since') or now))}: {TO} does not serve "
                       f"{h.get('model')}; runs start on the core provider until providers.{TO}.tiers.{tier}.model "
                       f"changes or the endpoint lists it (checked hourly)")
    if rec:
        ids = rec.get("ids") or []
        state_txt = "" if rec.get("ok") else f" (failed: {rec.get('check') or '?'}; ids from the last good check)"
        out.append(f"routing: {TO} served {len(ids)} model{'s' * (len(ids) != 1)} at {at(float(rec.get('checked') or now))}"
                   f"{state_txt}: {', '.join(ids) or '-'}")
    return out


def of(note) -> dict | None:
    """A run note's route entry (note: the dict or its JSON)."""
    if isinstance(note, str) or note is None:
        try:
            note = json.loads(note or "{}")
        except ValueError:
            return None
    e = note.get(KEY) if isinstance(note, dict) else None
    return e if isinstance(e, dict) else None


# The run's own error naming its model as refused (Claude Code's '[claude-code:unrecognized_model]
# {"model": ...}', an OpenAI-style model_not_found): the endpoint does not serve it.
MODEL_REFUSED = re.compile(r"\bunrecognized_model\b|\bmodel_not_found\b")


def fell_back(status: str, exit_info: dict, error: str = "") -> str:
    """Why a routed run on the endpoint fell back, or '' when it ended on its own merits; `error` is
    the run's own error text (a refused model there makes a bridge `error` or no fault model_missing)."""
    if status == "auth":
        return "auth"
    if status == "limit":
        return "rate_limit"
    if status not in FAULT_ENDS:
        return ""
    fault = str((exit_info.get("bridge") or {}).get("last_fault") or "")
    if fault in ("", "error") and MODEL_REFUSED.search(error or ""):
        return "model_missing"
    if fault in FAULTS:
        return fault
    if str(exit_info.get("launch_error") or "").find("chat bridge:") >= 0:
        return "error"   # the bridge could not start (not a key fault: that is auth)
    return ""


def _objects(text: str) -> list[str]:
    """Each top-level {...} span of `text`, braces inside JSON strings ignored; an unclosed one counts
    too, so a cut-off object is never mistaken for none."""
    spans, depth, start, quoted, escaped = [], 0, 0, False, False
    for i, ch in enumerate(text):
        if quoted:
            escaped, quoted = (False, True) if escaped else (ch == "\\", ch != '"')
        elif ch == '"' and depth:
            quoted = True
        elif ch == "{":
            start = i if depth == 0 else start
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                spans.append(text[start:i + 1])
    if depth:
        spans.append(text[start:])
    return spans


def salvage(final_text: str, states) -> dict | None:
    """The hand-off a routed run printed in its final reply instead of writing result.json: its one
    top-level JSON object when that is a hand-off (status in `states`), else None (none, several, or
    one that does not parse or pass)."""
    spans = _objects(final_text or "")
    if len(spans) != 1:
        return None
    try:
        data = json.loads(spans[0])
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("status") in states else None


def own_cause(status: str) -> str:
    """Why a routed run that handed off nothing falls back on its own (RUN_CAUSES), '' for an end that
    is no fallback (killed, out of budget, lost, a limit or the auth gate)."""
    return {"timeout": "timeout", "stalled": "timeout", "failed": "failed", "request_error": "failed",
            "ok": "no_handoff"}.get(status, "")


def fell_back_before(db, task_id: int) -> int | None:
    """The task's routed run that fell back on its own (RUN_CAUSES), or None: its later runs start on
    the core provider."""
    for r in db.q("SELECT id, provider, note FROM runs WHERE task=? AND role='worker' AND note LIKE ? "
                  "ORDER BY id DESC", (task_id, f'%"{KEY}"%')):
        e = of(r["note"]) or {}
        if e.get("fallback") in RUN_CAUSES and r["provider"] == e.get("to"):
            return int(r["id"])
    return None


def price(cfg: dict, prov: str, model: str, usage) -> bool:
    """Book a routed run's spend at `pricing.<prov>` ([$/M input, cached input, output] for the model
    or "default"); with no row the spend is zero and the run is unpriced. Returns whether it was priced."""
    rows = (cfg.get("pricing") or {}).get(prov) or {}
    row = rows.get(model) if isinstance(rows, dict) else None
    row = row if isinstance(row, (list, tuple)) and len(row) == 3 else (rows.get("default") if isinstance(rows, dict) else None)
    if not (isinstance(row, (list, tuple)) and len(row) == 3):
        usage.cost_usd = 0.0
        return False
    i, c, o = (float(x) for x in row)
    usage.cost_usd = round((usage.input_tokens * i + usage.cache_write_tokens * i + usage.cache_read_tokens * c
                            + usage.output_tokens * o) / 1e6, 6)
    return True


def effort_entry(stats: dict) -> dict | None:
    """The route entry's `effort` record from a bridge's stats: the effort, the fields sent and whether
    the endpoint rejected them; None when the run sent none."""
    fields = stats.get("fields")
    if not isinstance(fields, dict) or not fields:
        return None
    return {"effort": str(stats.get("effort") or ""), "fields": fields, "rejected": bool(stats.get("rejected"))}


def upstream_entry(stats: dict) -> dict | None:
    """The route entry's `upstream` record from a bridge's stats: the last failed answer's HTTP status
    and error code (chatbridge.upstream_of), each re-checked; None without one."""
    from .chatbridge import CODE_RE
    u = stats.get("upstream") if isinstance(stats, dict) else None
    status = u.get("status") if isinstance(u, dict) else None
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status < 600:
        return None
    code = u.get("code")
    return {"status": status, **({"code": code} if isinstance(code, str) and CODE_RE.fullmatch(code) else {})}


def fallback_line(run_id, e: dict) -> str:
    """A routed run's fallback for the coordinator feed, in place of its error text: 'routed run 12 on
    chat (model) fell back: error (HTTP 404 model_not_found); requeued at light on the core provider,
    no attempt spent'. The run that follows is the one whose outcome counts."""
    why = str(e.get("fallback") or "")
    u = e.get("upstream") if isinstance(e.get("upstream"), dict) else {}
    detail = " ".join(str(x) for x in (f"HTTP {u['status']}" if u.get("status") else "", u.get("code") or "") if x)
    cause = str(e.get("cause") or "")   # the daemon's own words only (_route_cause), never the run's error text
    detail = detail or (cause[:120] if why in RUN_CAUSES and cause.startswith(OWN_CAUSE_WORDS) else "")
    model = f" ({e['model']})" if e.get("model") else ""
    return (f"routed run {run_id} on {e.get('to') or TO}{model} fell back: {why}"
            + (f" ({detail})" if detail else "")
            + f"; requeued at {e.get('tier') or 'its tier'} on the core provider, no attempt spent")


def _fields_text(e: dict) -> str:
    """'standard high: reasoning_effort=xhigh' (with ', rejected' when the endpoint refused them)."""
    f = ", ".join(f"{k}={json.dumps(v) if not isinstance(v, str) else v}" for k, v in e["fields"].items())
    return f"{e.get('effort') or '-'}: {f}" + (" (rejected)" if e.get("rejected") else "")


def _rows(db, since: float, until: float | None = None) -> list[dict]:
    out = []
    for r in db.q("SELECT id, task, provider, model, status, started, ended, cost_usd, note FROM runs "
                  "WHERE role='worker' AND started>=? AND started<? AND note LIKE ? ORDER BY id",
                  (since, until or time.time() + 1, f'%"{KEY}"%')):
        e = of(r["note"])
        if e:
            out.append({**{k: r[k] for k in ("id", "task", "provider", "model", "status", "cost_usd")},
                        "tier": e.get("tier"), "to": e.get("to"), "fallback": e.get("fallback") or "none",
                        "priced": e.get("priced"), "salvaged": bool(e.get("salvaged")), "waited_s": e.get("waited_s"),
                        "effort": e.get("effort") if isinstance(e.get("effort"), dict) else None,
                        "wall_s": round(float(r["ended"]) - float(r["started"]), 1) if r["ended"] and r["started"] else None})
    return out


def phrase(db, now: float | None = None) -> str:
    """The last 24 h of routing in one short phrase for `ttp status` and the web budget line
    ('routed 24h: 6 runs to chat, 2 fell back (down 1, rate_limit 1), 1 hand-off salvaged; light high:
    reasoning_effort=xhigh',
    each routed tier's latest effort fields last), '' with none."""
    now = now or time.time()
    rows = _rows(db, now - 86400, now + 1)
    if not rows:
        return ""
    on = [r for r in rows if r["provider"] == r["to"]]
    fell = [r for r in rows if r["fallback"] not in ("none", "rerun")]
    reruns = sum(1 for r in rows if r["fallback"] == "rerun")   # the same task starting again, not a new fallback
    to = ", ".join(sorted({str(r["to"]) for r in rows}))
    text = f"routed 24h: {len(on)} run{'s' if len(on) != 1 else ''} to {to}"
    if fell:
        why: dict[str, int] = {}
        for r in fell:
            why[r["fallback"]] = why.get(r["fallback"], 0) + 1
        text += f", {len(fell)} fell back ({', '.join(f'{k} {n}' for k, n in sorted(why.items()))})"
    if reruns:
        text += f", {reruns} rerun{'s' if reruns != 1 else ''} on core"
    if waited := sum(1 for r in on if r.get("waited_s")):   # got a full slot after waiting (route_wait_s)
        text += f", {waited} after a slot wait"
    if salvaged := sum(1 for r in on if r.get("salvaged")):   # hand-offs kept from a final reply (salvage)
        text += f", {salvaged} hand-off{'s' if salvaged != 1 else ''} salvaged"
    last = {}   # each routed tier's latest effort fields (effort_fields), as the endpoint got them
    for r in on:
        if r["effort"]:
            last[str(r["tier"])] = r["effort"]
    if last:
        text += "; " + "; ".join(f"{t} {_fields_text(e)}" for t, e in sorted(last.items()))
    return text


def runs(db, tier: str | None, since: float, until: float | None = None) -> list[dict]:
    """Worker runs with their provider, model, tier, outcome and wall time (`ttp stats --runs`): every
    worker run of the window, routed or not, so a tier's runs compare before and after routing."""
    routed = {r["id"]: r for r in _rows(db, since, until)}
    out = []
    for r in db.q("SELECT r.id, r.task, r.provider, r.model, r.status, r.started, r.ended, r.cost_usd, r.note, "
                  "t.tier task_tier FROM runs r LEFT JOIN tasks t ON t.id=r.task WHERE r.role='worker' "
                  "AND r.started>=? AND r.started<? AND r.status!='running' ORDER BY r.id",
                  (since, until or time.time() + 1)):
        e = routed.get(r["id"])
        try:
            n = json.loads(r["note"] or "{}")
        except ValueError:
            n = {}
        # The run's tier: routed runs record it; else a wake's or a pick's, else the task's tier now.
        t = (e or {}).get("tier") or (n.get("wake") or {}).get("tier") or (n.get("pick") or {}).get("tier") \
            or r["task_tier"] or ""
        if tier and t != tier:
            continue
        out.append({"id": r["id"], "task": r["task"], "tier": t, "provider": r["provider"], "model": r["model"],
                    "outcome": r["status"], "fallback": (e or {}).get("fallback", ""),
                    "salvaged": bool((e or {}).get("salvaged")),
                    "priced": (e or {}).get("priced"), "cost_usd": round(float(r["cost_usd"] or 0), 4),
                    "wall_s": round(float(r["ended"]) - float(r["started"]), 1) if r["ended"] and r["started"] else None})
    return out


def runs_text(rows: list[dict]) -> str:
    if not rows:
        return "no worker runs in the window"
    lines = ["run    task   tier      provider  model                          outcome                wall      cost"]
    for r in rows:
        cost = "unpriced" if r["priced"] is False else f"${r['cost_usd']:.2f}"
        out = r["outcome"] + (f" ({r['fallback']})" if r["fallback"] not in ("", "none") else "") \
            + (" (salvaged)" if r.get("salvaged") else "")
        wall = f"{r['wall_s'] / 60:.1f}m" if r["wall_s"] is not None else "-"
        lines.append(f"{r['id']:<6} {str(r['task'] or ''):<6} {str(r['tier'] or '-'):<9} {str(r['provider']):<9} "
                     f"{str(r['model'] or '-')[:30]:<30} {out[:22]:<22} {wall:>6} {cost:>9}")
    return "\n".join(lines)


def config_problems(raw: dict) -> list[str]:
    """What is wrong with project.json's routing settings (none when it has none)."""
    provs = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    out = []
    for name, entry in provs.items():
        tiers = (entry or {}).get("tiers") if isinstance(entry, dict) else None
        for tier, t in (tiers.items() if isinstance(tiers, dict) else ()):
            if not isinstance(t, dict) or "route" not in t:
                continue
            if name != TO:
                out.append(f"providers.{name}.tiers.{tier}.route: only providers.{TO} tiers can be routed; ignored")
            elif not isinstance(t["route"], bool):
                out.append(f"providers.{TO}.tiers.{tier}.route: give true or false; not routed")
            elif t["route"] and not str(t.get("model") or ""):
                out.append(f"providers.{TO}.tiers.{tier}.route: set providers.{TO}.tiers.{tier}.model too; not routed")
            elif t["route"] and (raw.get("core_provider") or "claude") == TO:
                out.append(f"providers.{TO}.tiers.{tier}.route: {TO} is the core provider; nothing to route")
    chat = provs.get(TO) if isinstance(provs.get(TO), dict) else {}
    for tier, t in ((chat.get("tiers") or {}).items() if isinstance(chat.get("tiers"), dict) else ()):
        if not isinstance(t, dict):
            continue
        v = t.get("route_timeout_s")
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
            out.append(f"providers.{TO}.tiers.{tier}.route_timeout_s: give seconds (more than 0); "
                       "the tier's budget.run_timeout_s is used")
        if t.get("route") is True and str(t.get("model") or "") and (raw.get("core_provider") or "claude") != TO:
            from .chatbridge import effort_fields
            if not effort_fields(chat, str(t["model"]), str(t.get("effort") or "")):
                out.append(f"providers.{TO}.tiers.{tier}: its routed requests carry no reasoning effort; set "
                           f"providers.{TO}.tiers.{tier}.effort and providers.{TO}.effort_fields.<model>.<effort> "
                           '(such as {"reasoning_effort": "low"})')
    v = chat.get("route_cooldown_s")
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
        out.append(f"providers.{TO}.route_cooldown_s: give seconds (0 or more); {COOLDOWN_S} is used")
    return out


def wait_s(cfg: dict) -> float:
    """How long a routed start waits for a slot (providers.chat.route_wait_s, else WAIT_S; 0: none)."""
    v = ((cfg.get("providers") or {}).get(TO) or {}).get("route_wait_s")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else WAIT_S


def host_limit() -> int:
    """Routed runs per provider at once on this host: TTP_ROUTE_HOST_MAX, else the account's
    route.host_max, else HOST_MAX; 0 means no limit, an unreadable value the default."""
    from . import project
    for v in (os.environ.get(HOST_ENV), (project.load_account_settings().get("route") or {}).get("host_max")):
        try:
            n = int(str(v).strip()) if v not in (None, "") else -1
        except ValueError:
            n = -1
        if n >= 0:
            return n
    return HOST_MAX


def _host_file(prov: str) -> Path:
    from . import project
    return project.HOME_DIR / HOST_DIR / f"{prov}.json"


def _held(lease, now: float) -> bool:
    """A lease still stands: its process runs (the same one: its start token matches) and its run's
    time limit plus LEASE_GRACE_S has not passed."""
    from . import runner
    if not isinstance(lease, dict) or float(lease.get("until") or 0) < now:
        return False
    from .liveness import pid_state, DEAD, UNKNOWN
    try:
        pid = int(lease.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if (st := pid_state(pid)) == DEAD:
        return False
    if st == UNKNOWN:
        return True   # another PID namespace: its start token cannot be read here either
    start = lease.get("start")
    return not start or runner.proc_start(pid) == start


def _read(path: Path, now: float) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if _held(v, now)} if isinstance(data, dict) else {}


@contextmanager
def _locked(prov: str):
    path = _host_file(prov)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path


def _write(path: Path, leases: dict) -> None:
    from . import project
    project.durable_write(path, json.dumps(leases))


def host_used(prov: str, now: float | None = None) -> int:
    """Live leases on `prov` on this host (an unreadable file: none)."""
    return len(_read(_host_file(prov), now or time.time()))


def host_full(prov: str, now: float | None = None) -> bool:
    n = host_limit()
    return bool(n) and host_used(prov, now) >= n


def host_take(prov: str, key: str, pid: int, until: float, now: float | None = None) -> bool:
    """Take (or keep) the lease `key` on `prov` for process `pid` until `until`: False when the host's
    slots are all held by others. Stale leases are dropped on the way. A lease file that cannot be
    written raises OSError (the caller decides)."""
    from . import runner
    now = now or time.time()
    with _locked(prov) as path:
        leases = _read(path, now)
        n = host_limit()
        if key not in leases and n and len(leases) >= n:
            return False
        leases[key] = {"pid": pid, "start": runner.proc_start(pid) or "", "until": until, "ts": now}
        _write(path, leases)
    return True


def host_release(prov: str, key: str) -> None:
    """Free the lease `key` on `prov` (none: nothing to do)."""
    with _locked(prov) as path:
        leases = _read(path, time.time())
        if leases.pop(key, None) is not None or path.exists():
            _write(path, leases)
