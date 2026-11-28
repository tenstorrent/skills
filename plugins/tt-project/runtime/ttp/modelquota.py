"""Per-model quota buckets on the chat endpoint (release flag `glm_quota_alt`, rollout.py; off by default).

A quota 429 (chatbridge.quota_until) on a model, or an answer whose per-model rate-limit header says no tokens
remain, marks that model's bucket empty until its reset, in one account-wide file (~/.tt-project/quota/_models.json,
under a lock) every project on the host reads; an entry past its reset is ignored. The 429 body names a resource
and a window, not a model: the bucket is the model the request asked for.

While the flag is on, no run is dispatched to a model whose bucket is empty: a routed run on the chat provider
takes the first model of the account setting providers.chat.quota_alternates (an ordered list, empty by default)
that the endpoint serves, is not down (modelrank.down) and whose own bucket has room; with none it starts on the
core provider as before (fallback `quota`). Coordinator, plan, question, review, conclusion and person-facing
(`reply_chat`) runs never take an alternate. A model whose id contains `flash` is taken only once its server
refusals (429/503 given up, rate-limit faults: count()) are under FLASH_MAX of at least FLASH_MIN_CALLS calls."""

from __future__ import annotations

import re
import time

from . import glmquota

FLAG = "glm_quota_alt"
KEY = "quota_alt"              # the route entry's {"from", "until", "why", "to"?, "n"?}
FILE = "_models"               # glmquota's file key: quota/_models.json
ALT_KEY = "providers.chat.quota_alternates"   # account setting: the ordered alternates list
FLASH_MAX, FLASH_MIN_CALLS = 0.05, 20
COUNT_S = 7 * 86400            # a model's call and refusal counts start over this often
_DUR = re.compile(r"^(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:(\d+)ms)?$")


def _hour_end(now: float) -> float:
    return (int(now) // 3600 + 1) * 3600.0


def _reset(headers, now: float, kind: str = "tokens") -> float:
    """The reset a rate-limit header gives for `kind` (tokens or requests: seconds, `6m0s`, `6ms` or an epoch),
    else the end of this UTC hour."""
    for name in (f"x-ratelimit-reset-{kind}", "x-ratelimit-reset"):
        v = str(headers.get(name) or "").strip()
        if re.fullmatch(r"\d+(\.\d+)?", v):
            s = float(v)
            return s if s > 1e9 else now + s
        if v and (m := _DUR.match(v)) and any(m.groups()):
            h, mi, s, ms = (float(x or 0) for x in m.groups())
            return now + h * 3600 + mi * 60 + s + ms / 1000
    return _hour_end(now)


def mark(model: str, got: dict | None = None, now: float | None = None) -> None:
    """`model`'s bucket is empty until got['reset'] (else the end of this UTC hour), with the 429's used/limit."""
    if not model:
        return
    now = time.time() if now is None else now
    got = got or {}
    reset = float(got.get("reset") or 0)
    with glmquota._locked(FILE) as path:
        st = glmquota._read(path)
        st[model] = {**(st.get(model) or {}), "until": reset if reset > now else _hour_end(now), "at": now,
                     **{k: int(got[k]) for k in ("used", "limit") if isinstance(got.get(k), (int, float))}}
        glmquota._write(path, st)


def note(model: str, headers, now: float | None = None) -> None:
    """An answer's per-model rate-limit headers: none left marks `model`'s bucket empty until their reset."""
    if headers is None or not model:
        return
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    resets = [_reset(h, time.time() if now is None else now, kind) for kind in ("tokens", "requests")
              if h.get(f"x-ratelimit-remaining-{kind}", "").strip() == "0"]
    if resets:
        mark(model, {"reset": max(resets)}, now)


def count(model: str, calls: int, refusals: int, now: float | None = None) -> None:
    """Add a finished chat run's calls and server refusals to `model`'s counts (the flash rule)."""
    if not model or calls <= 0:
        return
    now = time.time() if now is None else now
    with glmquota._locked(FILE) as path:
        st = glmquota._read(path)
        m = st.get(model) or {}
        c = m.get("calls") if now - float((m.get("calls") or {}).get("since") or 0) < COUNT_S else None
        c = c or {"since": now, "n": 0, "refused": 0}
        st[model] = {**m, "calls": {**c, "n": c["n"] + calls, "refused": c["refused"] + refusals}}
        glmquota._write(path, st)


def _state() -> dict:
    return glmquota._read(glmquota._file(FILE))


def until(model: str, now: float | None = None, st: dict | None = None) -> float:
    """When `model`'s bucket refills, or 0 when it has room."""
    now = time.time() if now is None else now
    t = float(((_state() if st is None else st).get(model) or {}).get("until") or 0)
    return t if t > now else 0.0


def alternates() -> list[str]:
    from . import project
    v = project.account_value(ALT_KEY)
    return [str(m) for m in v if isinstance(m, str) and m] if isinstance(v, list) else []


def eligible(task: dict, cls: str | None) -> bool:
    """Whether a run may take an alternate: never a coordinator, plan, question, review, conclusion or chat reply."""
    return cls != "coordinator" and str(task.get("kind") or "") not in glmquota.SHORT_KINDS \
        and not task.get("reply_chat") and not glmquota.CONCLUSION.search(str(task.get("spec") or ""))


def _flash_ok(m: dict) -> bool:
    c = m.get("calls") or {}
    return int(c.get("n") or 0) >= FLASH_MIN_CALLS and int(c.get("refused") or 0) < FLASH_MAX * int(c["n"])


def room(db, cfg: dict, state, model: str, now: float, st: dict | None = None) -> tuple[int, str] | None:
    """(place from 1, model) of the first listed alternate other than `model` that is served, not down and has
    room, else None."""
    from . import modelrank
    st = _state() if st is None else st
    ids = modelrank.served(state, now) or []
    for n, m in enumerate(alternates(), 1):
        if m != model and m in ids and not until(m, now, st) and not modelrank.down(db, cfg, state, m, now) \
                and ("flash" not in m.lower() or _flash_ok(st.get(m) or {})):
            return n, m
    return None


def route(db, cfg: dict, state, model: str, task: dict, cls: str | None, now: float, fmt=None) -> dict | None:
    """None while `model`'s bucket has room; else the route entry's KEY record: {"from", "until", "why"} plus
    "to" and "n" (its place in the list, from 1) when an alternate has room. `fmt` shows a time."""
    st = _state()
    t = until(model, now, st)
    if not t:
        return None
    fmt = fmt or (lambda x: time.strftime("%H:%M UTC", time.gmtime(x)))
    rec = {"from": model, "until": t, "why": f"{model} quota bucket empty until {fmt(t)}"}
    if eligible(task, cls) and (got := room(db, cfg, state, model, now, st)):
        n, m = got
        return {**rec, "to": m, "n": n, "why": f"{rec['why']}: alternate {n}, {m}"}
    return {**rec, "why": f"{rec['why']}: no alternate has room, fallback to the core provider"}
