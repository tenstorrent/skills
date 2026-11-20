# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model ranking refresher: keeps the account's providers.chat.model_ranking current, model-free.

Every project's daemon whose chat provider has an endpoint (providers.chat.endpoint) and a good /models
check (route.MODELS_FILE) asks, without network, whether a refresh is due (due): the served list holds a
model no earlier refresh saw ("new model"), or the last good refresh is ranking_every_s old ("weekly"), or
the last try failed RETRY_S ago (its first trigger again). Nothing turns it on. One daemon on the host
refreshes at a time (LOCK); the others then find STATE current.

A refresh (refresh) fetches the Artificial Analysis Intelligence Index from ranking_source (its data API;
the key goes in the x-api-key header, from KEY_ENV or the file ranking_key_file names, and is never
logged or stored) and ranks every served id seen so far by it (rank): ids and index entries are
normalised (keys) and an id takes the best score of the entries it matches; ids none match go last,
sorted, named in the log. The result is written as the account's providers.chat.model_ranking, which
every project on the host reads under its own value (modelrank.ranking). Each id's index score is kept in
STATE (scores): the model scoreboard's prior (modelscore.py), which writes the ranking instead once it has
published class rankings.

Each change appends one line to LOG (when, the source and its index version, old and new lists, the
trigger, the unmatched ids) and the daemon posts one low feed line; no change, no line. A failed fetch
(or an answer matching no served id) keeps the ranking, never empties it, and raises one observation per
outage (STATE's outage, with error true). No key: nothing is fetched; the account gets one user-only
item (NEEDS_KEY) until one is there. Data from Artificial Analysis needs attribution: the log and feed
line name it.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

KEY = "providers.chat.model_ranking"
CLASS_KEY = "providers.chat.class_ranking"   # set by the model scoreboard (modelscore.py): it then writes KEY
SOURCE_KEY, EVERY_KEY, KEY_FILE_KEY = ("providers.chat.ranking_source", "providers.chat.ranking_every_s",
                                       "providers.chat.ranking_key_file")
SOURCE = "https://artificialanalysis.ai/api/v2/language/models"
SOURCE_NAME = "Artificial Analysis Intelligence Index"
EVERY_S = 7 * 86400
RETRY_S = 3600          # a failed refresh's next try
MAX_PAGES = 20
KEY_ENV = "ARTIFICIAL_ANALYSIS_API_KEY"
STATE, LOG, LOCK = "model-ranking-state.json", "model-ranking.jsonl", "model-ranking.lock"   # in project.HOME_DIR
NEEDS_KEY = "model-ranking-key"   # the user-only alert (alerts.USER_BLOCKERS)
NEEDS_KEY_TEXT = (f"The chat model ranking needs an Artificial Analysis API key: put it in a file only you can read "
                  f"and run `ttp config --account {KEY_FILE_KEY} <path>` (or set {KEY_ENV} for the daemon). "
                  f"Until then the last ranking stays.")
# Name tokens that say how a model is served, not which model it is; and trailing variant tokens an index
# entry may carry (an id matching several takes the best score).
QUANT = {"fp4", "fp8", "fp16", "bf16", "int4", "int8", "awq", "gptq", "nvfp4", "mxfp4", "w4a16", "w8a8", "gguf"}
VARIANT = {"reasoning", "non", "thinking", "adaptive", "minimal", "low", "medium", "high", "xhigh"}


def _home() -> Path:
    from .project import HOME_DIR
    return HOME_DIR


def setting(dotted: str) -> Any:
    """An account-level refresher setting's effective value (the default when unset or malformed)."""
    from .project import account_value
    v = account_value(dotted)
    if dotted == SOURCE_KEY:
        return v if isinstance(v, str) and v.startswith("https://") else SOURCE
    if dotted == EVERY_KEY:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= RETRY_S else EVERY_S
    return v if isinstance(v, str) and v else None


def check_setting(dotted: str, v: Any) -> str:
    """Why `v` is no good for account key `dotted`, else ''."""
    if v in ("", None):
        return ""
    if dotted == KEY:
        from .modelrank import ranking_of
        try:
            ranking_of(v)
        except ValueError as e:
            return str(e)
    elif dotted == SOURCE_KEY and not (isinstance(v, str) and v.startswith("https://")):
        return f"{dotted}: give an https:// URL"
    elif dotted == EVERY_KEY and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < RETRY_S):
        return f"{dotted}: give seconds, at least {RETRY_S}"
    elif dotted == KEY_FILE_KEY and not (isinstance(v, str) and os.path.isabs(os.path.expanduser(v))):
        return f"{dotted}: give the absolute path of a file holding the key"
    return ""


def api_key() -> str | None:
    """The Artificial Analysis key: KEY_ENV, else the first line of ranking_key_file; None without one."""
    k = os.environ.get(KEY_ENV, "").strip()
    path = setting(KEY_FILE_KEY)
    if not k and path:
        try:
            k = Path(os.path.expanduser(path)).read_text(errors="replace").strip().splitlines()[0].strip()
        except (OSError, IndexError):
            k = ""
    return k or None


def configured(cfg: dict) -> bool:
    chat = (cfg.get("providers") or {}).get("chat")
    return isinstance(chat, dict) and bool(chat.get("endpoint"))


def keys(name: str) -> set[str]:
    """A model name's comparable forms: lower case, no org prefix or tag, serving tokens dropped, with and
    without trailing variant tokens."""
    s = name.lower().rsplit("/", 1)[-1].split(":", 1)[0]
    toks = [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in QUANT]
    out = {"-".join(toks)}
    while toks and toks[-1] in VARIANT:
        toks.pop()
        out.add("-".join(toks))
    return {k for k in out if k}


def read_state() -> dict:
    try:
        v = json.loads((_home() / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def due(served: list[str], now: float, st: dict | None = None) -> str:
    """The refresh's trigger ('new model', 'weekly' or a failed try's own), or '' when none is due."""
    st = read_state() if st is None else st
    if st.get("outage"):
        return str(st["outage"].get("trigger") or "weekly") if now - float(st.get("tried") or 0) >= RETRY_S else ""
    if st.get("refreshed") and set(served) - set(st.get("seen") or []):   # the first refresh is the schedule's
        return "new model"
    return "weekly" if now - float(st.get("refreshed") or 0) >= setting(EVERY_KEY) else ""


def _get(url: str, key: str) -> Any:
    req = urllib.request.Request(url, headers={"x-api-key": key, "Accept": "application/json",
                                               "User-Agent": "tt-project-model-ranking"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read(16 << 20))


def _page(url: str, page: int) -> str:
    if page == 1:
        return url
    u = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(u.query) + [("page", str(page))]
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))


def fetch(url: str, key: str, get: Callable[[str, str], Any] | None = None) -> tuple[list[tuple[set[str], float]], str]:
    """The index's entries as (name keys, score), and its version; ValueError on an answer of another shape."""
    out, version, get = [], "", get or _get
    for page in range(1, MAX_PAGES + 1):
        body = get(_page(url, page), key)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError("unexpected answer")
        version = version or str(body.get("intelligence_index_version") or "")
        for m in body["data"]:
            ev = m.get("evaluations") if isinstance(m, dict) else None
            score = (ev or {}).get("artificial_analysis_intelligence_index") if isinstance(ev, dict) else None
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                names = [m.get(k) for k in ("slug", "name", "openrouter_api_id") if isinstance(m.get(k), str)]
                out.append((set().union(*map(keys, names)) if names else set(), float(score)))
        if not (body.get("pagination") or {}).get("has_more"):
            break
    if not out:
        raise ValueError("no ranked models")
    return out, version


def rank(served: list[str], entries: list[tuple[set[str], float]]) -> tuple[list[str], list[str]]:
    """(served ids smartest first, the matched by best score then the rest sorted; the ids none matched)."""
    scored, unmapped = [], []
    for sid in dict.fromkeys(served):
        k = keys(sid)
        best = max((s for ek, s in entries if ek & k), default=None)
        (unmapped.append(sid) if best is None else scored.append((-best, sid)))
    return [sid for _, sid in sorted(scored)] + sorted(unmapped), sorted(unmapped)


def _err(e: BaseException) -> str:
    """A fault's kind, never its text (a URL or header could be in it)."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    return str(e) if isinstance(e, ValueError) else type(e).__name__


def _write_state(st: dict) -> None:
    from .project import durable_write
    durable_write(_home() / STATE, json.dumps(st, indent=1))


def scores(ids: list[str], entries: list[tuple[set[str], float]]) -> dict[str, float]:
    """{id: its best index score} of the ids an entry matches (modelscore.py's prior)."""
    return {sid: best for sid in ids if (best := max((s for ek, s in entries if ek & keys(sid)), default=None)) is not None}


def log_change(old: list, new: list, trigger: str, now: float, version: str = "", unmapped: list | None = None,
               by: str = "refresher") -> None:
    """One LOG line for a ranking change."""
    from .project import durable_append
    rec = {"ts": round(now, 3), "source": f"{SOURCE_NAME} ({setting(SOURCE_KEY)})" if by == "refresher" else by,
           "version": version, "old": list(old), "new": list(new), "trigger": trigger, "unmapped": list(unmapped or [])}
    durable_append(_home() / LOG, json.dumps(rec, sort_keys=True) + "\n")


def refresh(served: list[str], trigger: str, now: float, key: str,
            get: Callable[[str, str], Any] | None = None) -> dict:
    """One refresh under LOCK; {} when another daemon holds it or did it already. Else {"changed", "old",
    "new", "trigger", "version", "unmapped"} or {"error", "outage_new", "since"}."""
    from .modelrank import ranking_of
    from .project import account_value, set_account_setting
    _home().mkdir(parents=True, exist_ok=True)
    with open(_home() / LOCK, "a") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {}
        st = read_state()
        trigger = due(served, now, st)
        if not trigger:
            return {}
        seen = sorted(set(st.get("seen") or []) | set(served))
        try:
            entries, version = fetch(setting(SOURCE_KEY), key, get)
            new, unmapped = rank(seen, entries)
            if len(unmapped) == len(new):
                raise ValueError("no served model matched the index")
        except Exception as e:   # the fetch or its answer: the ranking stays
            out = st.get("outage") or {"since": now, "trigger": trigger, "error": True}
            _write_state({**st, "tried": now, "outage": {**out, "why": _err(e)}})
            return {"error": _err(e), "outage_new": not st.get("outage"), "since": out["since"]}
        try:
            old = ranking_of(account_value(KEY))
        except ValueError:
            old = []
        if new != old and not account_value(CLASS_KEY):   # once the scoreboard ranks, its index scores are a prior
            set_account_setting(KEY, new)
            log_change(old, new, trigger, now, version, unmapped)
        else:
            new = old
        _write_state({"refreshed": now, "tried": now, "seen": seen, "version": version, "outage": None,
                      "scores": scores(seen, entries)})
        return {"changed": new != old, "old": old, "new": new, "trigger": trigger, "version": version,
                "unmapped": unmapped, "ended": bool(st.get("outage"))}


def change_text(res: dict) -> str:
    ver = f" (index {res['version']})" if res.get("version") else ""
    miss = f"; not in the index, ranked last: {', '.join(res['unmapped'])}" if res.get("unmapped") else ""
    return (f"Chat model ranking refreshed from the {SOURCE_NAME}{ver}, trigger {res['trigger']}: "
            f"{', '.join(res['new'])} (was {', '.join(res['old']) or 'unset'}){miss}. Logged in {_home() / LOG}.")
