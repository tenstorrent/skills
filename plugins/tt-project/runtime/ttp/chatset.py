# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The provider keys a coordinator config_set may write on the user's word (coordinator.settable),
beyond the fixed USER_SETTABLE list: per-tier routing (route.py) and the chat endpoint (chatbridge.py).

Worker runs carry internal code, so chat text never points them at a free-form URL:
- providers.chat.endpoint is accepted only as the alias of an endpoint already saved on this host
  with `ttp secret endpoint <alias> --url <base url>`; the key's URL is the one the bridge calls.
- providers.<p>.base_url is always refused: with it unset the bridge uses the URL saved with the key.
- providers.chat.effort_fields is accepted only as plain model -> effort -> {field: scalar} data.
Nothing here reads or returns a key: only whether one is saved.
"""
from __future__ import annotations

import json
import re

PROVIDERS = ("claude", "codex", "cursor", "chat")
TIERS = ("light", "standard", "deep")
TIER_KEYS = ("model", "effort", "route", "route_timeout_s")
ROUTE_TIMEOUT_MAX_S = 14400   # 4 h: twice the deep tier's default run limit
EFFORTS = ("", "minimal", "low", "medium", "high", "xhigh", "max")   # "" keeps the provider's default
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
FIELD_VALUE_MAX = 64
# Field names some gateways read as a destination or credential (api_base, base_url, api_key, extra_headers).
WIRING_RE = re.compile(r"url|base|host|endpoint|key|token|auth|header|proxy|secret|cred", re.I)
# The keys and patterns config_set accepts here, as `ttp config --chat-settable list` prints them.
PATTERNS = (*(f"providers.<{'|'.join(PROVIDERS)}>.tiers.<{'|'.join(TIERS)}>.{k}" for k in ("model", "effort")),
            *(f"providers.chat.tiers.<{'|'.join(TIERS)}>.{k}" for k in ("route", "route_timeout_s")),
            "providers.chat.endpoint", "providers.chat.effort_fields")
BASE_URL_REFUSAL = ("{key} is never set from chat: the bridge sends requests only to the base URL saved with "
                    "the endpoint's key; save it with `ttp secret endpoint <alias> --url <base url>` on this host, "
                    "then config_set providers.chat.endpoint to <alias>")


def _model(key: str):
    def conv(v):
        v = "" if v is None else v
        if not isinstance(v, str) or (v and (not MODEL_RE.match(v) or "://" in v)):
            raise ValueError(f"{key}: give a model name (letters, digits and . _ : / @ + -, up to 128), "
                             f"or \"\" for the provider's default; not a URL")
        return v
    return conv


def _effort(key: str, provider: str):
    allowed = EFFORTS if provider != "chat" else ("", "low", "medium", "high", "xhigh", "max")
    def conv(v):
        v = "" if v is None else v
        if not isinstance(v, str) or v not in allowed:
            raise ValueError(f"{key}: give one of {', '.join(repr(x) for x in allowed)}")
        return v
    return conv


def _route(key: str):
    def conv(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
        raise ValueError(f"{key}: give true or false")
    return conv


def _route_timeout(key: str):
    def conv(v):
        try:
            n = float(v) if not isinstance(v, bool) and isinstance(v, (int, float, str)) else None
        except ValueError:
            n = None
        if n is None or not 0 < n <= ROUTE_TIMEOUT_MAX_S or n != n:
            raise ValueError(f"{key}: give seconds, more than 0 and at most {ROUTE_TIMEOUT_MAX_S}")
        return int(n) if n == int(n) else n
    return conv


def _endpoint(v):
    from . import endpoint
    alias = v.strip() if isinstance(v, str) else ""
    try:
        endpoint.slot(alias)
    except ValueError:
        raise ValueError("providers.chat.endpoint: give the alias of an endpoint saved with "
                         "`ttp secret endpoint <alias> --url <base url>`, never a URL") from None
    if endpoint.credentials(alias) is None:
        raise ValueError(f"providers.chat.endpoint: no endpoint {alias!r} is saved on this host; save its key with "
                         f"`ttp secret endpoint {alias} --url <base url>` first (`ttp secret show` lists them)")
    return alias


def _effort_fields(v):
    from .chatbridge import _effort_problems
    where = "providers.chat.effort_fields"
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            pass
    if not isinstance(v, dict) or not v:
        raise ValueError(f"{where}: give an object of model names, each an object of efforts, each an object "
                         'of request fields, such as {"<model>": {"low": {"reasoning_effort": "low"}}}')
    if bad := _effort_problems(v):
        raise ValueError(bad[0])
    for model, row in v.items():
        if not isinstance(model, str) or not MODEL_RE.match(model) or "://" in model:
            raise ValueError(f"{where}: {model!r} is not a model name")
        for effort, fields in row.items():
            for name, val in fields.items():
                if not FIELD_RE.match(name) or WIRING_RE.search(name):
                    raise ValueError(f"{where}.{model}.{effort}: {name!r} is not a plain field name "
                                     "(nothing naming a URL, host, key, token or header)")
                if not (isinstance(val, (bool, int, float)) or (isinstance(val, str) and len(val) <= FIELD_VALUE_MAX
                                                                 and "://" not in val)):
                    raise ValueError(f"{where}.{model}.{effort}.{name}: give a number, true/false or a short "
                                     "word, never an object, a list or a URL")
    return v


def setter(key: str):
    """The validator config_set uses for a provider key it accepts here, or None."""
    parts = key.split(".")
    if len(parts) == 5 and parts[0] == "providers" and parts[1] in PROVIDERS and parts[2] == "tiers" \
            and parts[3] in TIERS and parts[4] in TIER_KEYS:
        prov, last = parts[1], parts[4]
        if last == "model":
            return _model(key)
        if last == "effort":
            return _effort(key, prov)
        if prov == "chat":
            return _route(key) if last == "route" else _route_timeout(key)
        return None
    if key == "providers.chat.endpoint":
        return _endpoint
    if key == "providers.chat.effort_fields":
        return _effort_fields
    return None


def refusal(key: str) -> str | None:
    """Why config_set refuses a provider key, when there is more to say than 'not user-settable'."""
    parts = key.split(".")
    if parts[0] != "providers" or len(parts) < 3:
        return None
    if parts[-1] == "base_url":
        return BASE_URL_REFUSAL.format(key=key)
    if len(parts) == 3 and parts[2] == "endpoint":
        return f"{key}: only providers.chat reads an endpoint"
    if len(parts) >= 4 and parts[2] == "tiers":
        if parts[1] not in PROVIDERS:
            return f"{key}: unknown provider {parts[1]!r}; give one of {', '.join(PROVIDERS)}"
        if parts[3] not in TIERS:
            return f"{key}: unknown tier {parts[3]!r}; give one of {', '.join(TIERS)}"
        if len(parts) == 5 and parts[4] in ("route", "route_timeout_s"):
            return f"{key}: only providers.chat tiers can be routed"
        return (f"{key} is not user-settable from chat; per tier it takes "
                f"{', '.join(TIER_KEYS)} (route and route_timeout_s on providers.chat only)")
    return None


def route_ready(cfg: dict, key: str, value) -> None:
    """ValueError when config_set would turn a tier's routing on before its model and endpoint are set:
    set route last, so a tier never goes live half-configured."""
    parts = key.split(".")
    if not (len(parts) == 5 and parts[1] == "chat" and parts[4] == "route" and value is True):
        return
    chat = ((cfg.get("providers") or {}).get("chat") or {}) if isinstance(cfg.get("providers"), dict) else {}
    tier = ((chat.get("tiers") or {}).get(parts[3]) or {}) if isinstance(chat, dict) else {}
    missing = [k for k, have in ((f"providers.chat.tiers.{parts[3]}.model", isinstance(tier, dict) and tier.get("model")),
                                 ("providers.chat.endpoint", isinstance(chat, dict) and chat.get("endpoint")))
               if not have]
    if missing:
        raise ValueError(f"{key} true: set {' and '.join(missing)} first (route goes last)")
