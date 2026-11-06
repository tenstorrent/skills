# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Cursor's light, standard and deep tiers as models from the installed CLI's own list.

Cursor takes no effort flag (providers/cursor.py): a tier differs from another only by its model.
The shipped tiers name `auto` for all three, which would make a deep run the same as a light one.
So the harness asks the installed CLI (`agent --list-models`, which `agent --help` lists) for the
models this account may use and picks one per tier by this rule, never by a hard-coded model name:

- `auto` (Cursor's own router) is never picked; it stays the fallback.
- Each listed model is ranked by the class words in its id and display name: reasoning words
  (thinking, reasoning, high, xhigh, max) mark a reasoning model; strength words (pro, max, ultra,
  large, xhigh, high, thinking, reasoning) count up, cheap words (mini, nano, lite, flash, fast,
  small, low, light, tiny) count down. Ties keep the CLI's listing order.
- deep: the strongest reasoning model (the strongest model when none is marked reasoning).
- light: the cheapest model (fewest strength and most cheap words), not deep's.
- standard: the account's default model when it is listed and neither deep's nor light's pick, else
  the model ranked midway between them. With fewer than three models, tiers share them.

The resolved map is kept host-wide in `cursor-tiers.json` under the tt-project home with the CLI's
version, and resolved again when the CLI binary (an upgrade) or its login changes, once it is
LISTED_MAX_S old (a model withdrawn on the server, a changed plan), and at once after a run failed
because the CLI rejected a model the map named (`drop_rejected`; that model stays out of the map until
the CLI or its login changes). A listing that fails (logged out,
an old CLI without `--list-models`, no answer) leaves the tiers on `auto` and says why; it is asked
again after RETRY_S or at once on a login or CLI change. An aged map whose refresh fails for any
reason but a logout keeps its models and is asked again after RETRY_S.

A resolved model with no pricing.cursor row is priced at the top rate (providers/cursor.py `rate`);
`ttp doctor` names the rate each tier's spend and budget cap use (`rate_line`).

A project's own providers.cursor.tiers.<tier>.model in project.json, `auto` included, always wins
(`apply`), as does a task's own model pin (daemon.start_run).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .providers.base import binary_stamp, cli_output, find_binary, status_check

TIERS = ("light", "standard", "deep")
FALLBACK = "auto"
CACHE_NAME = "cursor-tiers.json"
RETRY_S = 3600.0                 # a failed listing is asked again after this long (or on a login/CLI change)
LISTED_MAX_S = 86400.0           # a listed map is asked again once this old
LIST_TIMEOUT_S = 30.0
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# One listed model: '<id> - <display name> (current, default)' (the name and the marks are optional).
LINE_RE = re.compile(r"^(?P<id>[A-Za-z0-9][\w.:/\[\]=,-]*)(?:\s+-\s+(?P<name>.*?))?"
                     r"(?:\s+\((?P<marks>(?:current|default)(?:,\s*(?:current|default))?)\))?\s*$")
REASONING = {"thinking", "reasoning", "high", "xhigh", "max"}
STRONG = {"pro": 2, "max": 2, "ultra": 2, "large": 1, "xhigh": 2, "high": 1, "thinking": 1, "reasoning": 1}
CHEAP = {"mini": 2, "nano": 3, "lite": 2, "flash": 2, "fast": 1, "small": 2, "low": 1, "light": 1, "tiny": 3}
# The CLI refusing the model a run named (withdrawn, not on the plan, unknown), on the run's error.
REJECT_RE = re.compile(r"cannot use (?:this|the) model|(?:unknown|invalid|unsupported|unavailable) model|"
                       r"model\b[^\n]{0,80}?\b(?:not (?:found|available|supported|allowed|enabled)|unavailable|"
                       r"does not exist|is not valid|no longer)", re.I)
LOGIN_RE = re.compile(r"authentication required|not (?:logged|authenticated|signed) in|agent login|"
                      r"credentials or api key are invalid", re.I)


def cache_path() -> Path:
    from .project import HOME_DIR
    return HOME_DIR / CACHE_NAME


def parse_models(text: str) -> list[dict]:
    """The models `agent --list-models` prints: [{"id", "name", "default"}], in its order."""
    out = []
    for line in ANSI_RE.sub("", text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("available models", "tip:", "no models", "error")):
            continue
        m = LINE_RE.match(line)
        if m and m["id"] not in {e["id"] for e in out}:
            out.append({"id": m["id"], "name": (m["name"] or "").strip(),
                        "default": "default" in (m["marks"] or "")})
    return out


def _words(model: dict) -> list[str]:
    return re.findall(r"[a-z]+", f"{model['id']} {model.get('name') or ''}".lower())


def strength(model: dict) -> int:
    """Strength words count up, cheap words down (each word once)."""
    words = set(_words(model))
    return sum(STRONG.get(w, 0) for w in words) - sum(CHEAP.get(w, 0) for w in words)


def reasoning(model: dict) -> bool:
    return bool(REASONING & set(_words(model)))


def pick(models: list[dict]) -> dict[str, str]:
    """{tier: model id} by the module's rule; {} when no model but `auto` is listed."""
    cands = [m for m in models if m["id"].lower() != FALLBACK]
    if not cands:
        return {}
    order = {m["id"]: i for i, m in enumerate(cands)}
    # Weakest first; among equals the earlier listed model counts as the stronger one.
    ranked = sorted(cands, key=lambda m: (reasoning(m), strength(m), -order[m["id"]]))
    reasoners = [m for m in ranked if reasoning(m)]
    deep = (reasoners or ranked)[-1]
    rest = [m for m in ranked if m is not deep]
    light = rest[0] if rest else deep
    middle = [m for m in ranked if m is not deep and m is not light]
    default = next((m for m in middle if m.get("default")), None)
    standard = default or (middle[len(middle) // 2] if middle else deep)
    return {"light": light["id"], "standard": standard["id"], "deep": deep["id"]}


def stamp(binary: str, credentials: str = "") -> str:
    """Path, mtime and size of the CLI binary as installed, and the login files' stamp: either moving
    (an upgrade, a login) resolves the map again."""
    return f"{binary_stamp(binary) or binary + ':-'}|{credentials}"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    from .project import durable_write
    try:
        durable_write(path, json.dumps(data, indent=1, sort_keys=True))
    except OSError:
        pass


def _list(binary: str) -> tuple[list[dict], str]:
    """(models, why not) from the CLI; why is "" when it listed models."""
    help_text = cli_output(binary, "--help")
    if "--list-models" in help_text:
        argv = [binary, "--list-models"]
    elif re.search(r"^\s*models\b", help_text, re.M):
        argv = [binary, "models"]
    else:
        return [], "this Cursor CLI lists no models (no --list-models in `agent --help`)"
    got = status_check(argv, timeout_s=LIST_TIMEOUT_S)
    if got is None:
        return [], f"`{os.path.basename(binary)} {argv[1]}` did not answer"
    rc, text = got
    if LOGIN_RE.search(text or ""):
        return [], "the Cursor CLI is logged out (`agent login`)"
    models = parse_models(text) if rc == 0 else []
    if not models:
        first = next((ln.strip() for ln in ANSI_RE.sub("", text or "").splitlines() if ln.strip()), "")
        return [], f"`{os.path.basename(binary)} {argv[1]}` exited {rc} listing no models" + (f": {first[:120]}" if first else "")
    return models, ""


def resolve(binary: str | None = None, credentials: str | None = None, now: float | None = None,
            path: Path | None = None, force: bool = False) -> dict:
    """The resolved map: {"source": "listed" | "fallback", "tiers": {tier: model}, "why", "cli", "at",
    "stamp"}. Cached host-wide; the CLI is asked again only when its stamp moved, after RETRY_S for a
    fallback, or with `force`."""
    now = time.time() if now is None else now
    path = path or cache_path()
    if binary is None:
        binary = find_binary("agent", "cursor-agent")
    why = "the Cursor CLI is not installed" if not binary else \
        "model listing is off in tests" if os.environ.get("TTP_TEST_NO_MODELS_PROBE") else ""
    if why:
        return {"source": "fallback", "tiers": dict.fromkeys(TIERS, FALLBACK), "why": why, "cli": "", "at": now, "stamp": ""}
    if credentials is None:
        from .providers import get_provider
        credentials = get_provider("cursor").credentials_stamp()
    key = stamp(binary, credentials)
    have = _read(path)
    same = not force and have.get("stamp") == key and isinstance(have.get("tiers"), dict) and not have.get("rejected")
    if same and (now - float(have.get("at") or 0) < (LISTED_MAX_S if have.get("source") == "listed" else RETRY_S)
                 or have.get("source") == "listed" and now - float(have.get("tried") or 0) < RETRY_S):
        return have
    got = status_check([binary, "--version"], timeout_s=15)
    version = (got[1].strip().splitlines() or [""])[0][:80] if got and got[0] == 0 else ""
    models, why = _list(binary)
    # A model the CLI refused stays out of the map until the CLI or its login changes.
    excluded = sorted(set(have.get("excluded") or []) | {have.get("rejected") or ""} - {""}) \
        if have.get("stamp") == key else []
    tiers = pick([m for m in models if m["id"] not in excluded])
    if not tiers:
        why = why or "the Cursor CLI lists no model but auto"
        if same and have.get("source") == "listed" and "logged out" not in why:
            # An aged map whose refresh failed keeps its models; asked again after RETRY_S.
            kept = {**have, "tried": now, "refresh_why": why}
            _write(path, kept)
            return kept
    out = {"source": "listed" if tiers else "fallback", "tiers": tiers or dict.fromkeys(TIERS, FALLBACK),
           "why": "" if tiers else why, "cli": version, "at": now, "stamp": key,
           "models": [m["id"] for m in models], **({"excluded": excluded} if excluded else {})}
    _write(path, out)
    return out


def model_rejected(text: str, model: str) -> bool:
    """Whether a failed run's error says the CLI refused its (named, not `auto`) model."""
    return bool(model and model.lower() != FALLBACK and text and REJECT_RE.search(text))


def drop_rejected(model: str, text: str, path: Path | None = None) -> bool:
    """After a failed run: when the CLI refused `model` and the kept listed map names it, mark the map
    so the next run lists the models again. True when it did."""
    path = path or cache_path()
    have = _read(path)
    if have.get("source") != "listed" or model not in (have.get("tiers") or {}).values() or \
            not model_rejected(text, model):
        return False
    _write(path, {**have, "rejected": model})
    return True


def own_tiers(raw: dict | None) -> set[str]:
    """The tiers whose model the project's own project.json sets."""
    tiers = (((raw or {}).get("providers") or {}).get("cursor") or {}).get("tiers") if isinstance(raw, dict) else None
    if not isinstance(tiers, dict):
        return set()
    return {t for t, row in tiers.items() if isinstance(row, dict) and "model" in row}


def apply(cfg: dict, raw: dict | None, resolved: dict) -> dict[str, str]:
    """Write the resolved models into cfg's providers.cursor.tiers, leaving each tier the project sets
    itself and any tier whose model is not the shipped `auto`. Returns {tier: model} it set."""
    tiers = ((cfg.get("providers") or {}).get("cursor") or {}).get("tiers")
    if not isinstance(tiers, dict) or resolved.get("source") != "listed":
        return {}
    mine = own_tiers(raw)
    done = {}
    for tier, model in (resolved.get("tiers") or {}).items():
        row = tiers.get(tier)
        if tier in mine or not isinstance(row, dict) or str(row.get("model") or "") != FALLBACK or not model:
            continue
        row["model"] = str(model)
        done[tier] = str(model)
    return done


def line(resolved: dict, raw: dict | None = None) -> str:
    """One `ttp doctor` line naming the map in force."""
    mine = own_tiers(raw)
    tiers = resolved.get("tiers") or {}
    parts = [f"{t} {'project.json' if t in mine else tiers.get(t, FALLBACK)}" for t in TIERS]
    cli = f" (CLI {resolved['cli']})" if resolved.get("cli") else ""
    if resolved.get("source") == "listed":
        return f"cursor tiers from the CLI's model list{cli}: " + ", ".join(parts)
    return f"cursor tiers: auto for every tier not set in project.json{cli}: {resolved.get('why') or 'no listing'}"


def rate_line(cfg: dict) -> str:
    """One `ttp doctor` line naming the rate each tier's spend estimate and budget cap use, after
    `apply` wrote the resolved models into cfg."""
    from .providers.cursor import rate
    prices = (cfg.get("pricing") or {}).get("cursor") or {}
    tiers = ((cfg.get("providers") or {}).get("cursor") or {}).get("tiers") or {}
    parts = []
    for t in TIERS:
        model = str((tiers.get(t) or {}).get("model") or "")
        (pin, pcached, pout), why = rate(model, prices)
        parts.append(f"{t} {model or 'default'} ${pin:g}/${pcached:g}/${pout:g} ({why})")
    return "cursor spend and budget cap rates per Mtok input/cached/output: " + "; ".join(parts)
