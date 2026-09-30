# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Jev (TypeSafe's System One decision model) as a cheap screener. Optional: every caller has a
rules fallback, and the project keeps working when no key is configured or funds run out.

Jev answers typed questions about a text "state": `noul` (probability of true), `choice` (one
option key plus probabilities) and `score` (a position on an ordered rubric). Each response
reports its exact cost. It does not generate text; anything needing prose goes to an LLM.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..project import load_secrets

# Endpoints are configuration, not code: both are overridable per project (`jev.url`).
ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/alpha/decisions",
    "typesafe": "https://api.typesafe.ai/v1/systemone",
}
DEFAULT_MODEL = {"openrouter": "~typesafe/jev-latest", "typesafe": "jev-latest"}
INPUT_USD_PER_MTOK = 0.042     # published list price; used only when the response carries no cost


class JevUnavailable(Exception):
    pass


class JevOutOfFunds(JevUnavailable):
    pass


class Jev:
    def __init__(self, cfg: dict, db=None, secrets: dict | None = None):
        self.cfg = cfg.get("jev", {}) or {}
        self.db = db
        sec = (secrets if secrets is not None else load_secrets()).get("jev") or {}
        self.via = self.cfg.get("via") or sec.get("via") or "typesafe"
        self.key = sec.get("key") or ""
        self.url = self.cfg.get("url") or sec.get("url") or ENDPOINTS.get(self.via, "")
        self.model = self.cfg.get("model") or DEFAULT_MODEL.get(self.via, "jev-latest")
        self._pause_until = 0.0

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled") and self.key and self.url and time.time() >= self._pause_until)

    def decide(self, state: str, questions: dict[str, Any], purpose: str = "decide",
               timeout: float = 20.0) -> dict[str, Any] | None:
        if not self.enabled():
            return None
        body = json.dumps({"model": self.model, "state": state, "questions": questions}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 402 or (e.code == 403 and "credit" in (e.read() or b"").decode(errors="replace").lower()):
                self._pause_until = time.time() + 3600
                raise JevOutOfFunds("Jev account has insufficient credits") from None
            if e.code in (429, 503, 529):
                self._pause_until = time.time() + 60
            raise JevUnavailable(f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise JevUnavailable(type(e).__name__) from None
        usage = data.get("usage") or {}
        # OpenRouter reports cost; TypeSafe reports tokens only (input priced, output free).
        cost = float(usage.get("cost") or 0.0) or int(usage.get("input_tokens") or 0) * INPUT_USD_PER_MTOK / 1e6
        if self.db is not None:
            self.db.spend("jev", cost, f"jev:{purpose}", account=self.via,
                          tokens_in=int(usage.get("input_tokens") or 0),
                          tokens_out=int(usage.get("output_tokens") or 0))
        return data.get("answers") or {}


def verify(via: str, key: str, url: str | None = None) -> tuple[bool, str]:
    """One tiny decision to prove a key works before it is saved. Returns (ok, message)."""
    j = Jev({"jev": {"enabled": True, "via": via, "url": url}}, db=None,
            secrets={"jev": {"via": via, "key": key, "url": url}})
    try:
        ans = j.decide("The sky is blue.", {"t": {"type": "noul", "instructions": "Is the statement true?",
                                                  "criteria": {"true": "true", "false": "false"}}},
                       purpose="verify")
        return (bool(ans), "ok" if ans else "empty answer")
    except JevOutOfFunds:
        return (False, "the key works but the account has no credits")
    except JevUnavailable as e:
        return (False, str(e))
