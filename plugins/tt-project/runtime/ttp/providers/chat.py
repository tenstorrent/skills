# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Claude Code workers on an OpenAI-compatible (Chat Completions) endpoint, through the run's
loopback bridge (chatbridge.py). Off until a project configures `providers.chat` and routes work
to it; the key stays in the `ttp secret endpoint <alias>` store."""
from __future__ import annotations

import re

from ..project import secrets_path
from . import register
from .base import Provider
from .claude import Claude

LOGIN_CHECK_TIMEOUT_S = 5   # check_logins asks it in the daemon's loop


@register
class Chat(Claude):
    name = "chat"
    # The agent reaches only the loopback bridge; the runner's bridge reaches the upstream.
    api_host, api_base_env = "", ""
    login_hint = "save the endpoint's key with `ttp secret endpoint <alias> --url <base url>`"
    own_cache_breakpoints = 0
    # Its coordinator turns run stateless for now (coordsession.py names this reason), with the
    # whole-wake protected-file guard: ttp's tool hooks and system-prompt flags are wired for claude.
    coord_session = ""
    session_gap = "the coordinator session is not enabled on this provider yet"
    tool_hooks = False

    # Claude Code prices a model name it does not know at its fallback rate. With pricing.chat rows
    # ({"<model id>" or "default": [input, cached input, output] in $ per million tokens}) a run is
    # repriced from its tokens and the runner enforces its budget on that figure (a zero row: free,
    # never cut); without them its spend is Claude Code's figure, booked as estimated, and Claude
    # Code's own cap stays, a fallback-rate guess (fallback_cap_line), unless providers.chat.billing
    # is plan: then no dollar cap at all, the runner's wall-clock and stall guards bound the run.
    @property
    def caps_budget(self) -> bool:
        return not self.price() and self.billing != "plan"

    def price(self) -> tuple[float, float, float] | None:
        from ..chatbridge import price_ok
        rows = {k: v for k, v in (self.prices or {}).items() if price_ok(v)}
        row = rows.get(self.model) or rows.get("default")
        return tuple(float(x) for x in row) if row else None

    def build(self, *, budget_usd, **kw):
        return super().build(budget_usd=budget_usd if self.caps_budget else None, **kw)

    def parse(self, output_path, stderr_path=None):
        u = super().parse(output_path, stderr_path)
        row = self.price()
        if row:
            pin, pcached, pout = row
            u.extra["reported_cost_usd"] = u.cost_usd
            u.cost_usd = round(((u.input_tokens + u.cache_write_tokens) * pin + u.cache_read_tokens * pcached
                                + u.output_tokens * pout) / 1e6, 6)
            u.estimated, u.extra["cost_basis"] = False, "pricing"
        else:
            u.estimated, u.extra["cost_basis"] = True, "fallback"
        return u

    def cost_so_far(self, output_path, since=None, env=None):
        return self.parse(output_path).cost_usd if self.price() else None

    def credential_files(self) -> list[str]:
        return [str(secrets_path())]   # a key saved again ends a logged-out pause

    def login_check(self) -> bool | None:
        """Model-free, given the project's providers.chat settings (`entry`): False while no key is saved
        for its endpoint alias (or the key's URL is not providers.chat.base_url, which the bridge
        refuses), or the endpoint's GET /models refuses the key (HTTP 401 or 403); True once it takes
        it. None without settings, while the store is unreadable or the endpoint does not answer: a run
        then checks. A logged-out breaker so never starts a run that fails at launch for want of a key."""
        if self.entry is None:
            return None
        from .. import endpoint
        from ..project import SecretsUnreadable
        alias = str(self.entry.get("endpoint") or "")
        try:
            got = endpoint.credentials(alias) if endpoint.ALIAS_RE.match(alias) else None
        except SecretsUnreadable:
            return None
        if got is None:
            return False
        try:
            if self.entry.get("base_url") and endpoint.base_url(self.entry["base_url"]) != endpoint.base_url(got.url):
                return False
        except ValueError:
            return False
        ok, msg = endpoint.verify(got.url, got.key, timeout=LOGIN_CHECK_TIMEOUT_S)
        if ok:
            return True
        return False if re.match(r"HTTP (401|403)\b", msg) else None

    def login_state(self, cfg: dict | None = None) -> bool | None:
        # The daemon's logged-out breaker reads this, so it runs login_check on the project's own
        # providers.chat settings: no key saved for the alias, or one saved for another URL (the bridge
        # refuses every run, chatbridge.Bridge.from_settings), or a key the endpoint refuses is False and
        # starts no probe run; a key it takes is True. None without an endpoint alias: a run then checks.
        s = ((cfg or {}).get("providers") or {}).get(self.name) if cfg is not None else self.entry
        if not isinstance(s, dict) or not s.get("endpoint"):
            return None
        prev, self.entry = self.entry, dict(s)
        try:
            return self.login_check()
        except Exception:   # a bad URL or setting: a run reports it
            return None
        finally:
            self.entry = prev

    def resolved_session(self, argv, cwd, env=None):
        # The bridge sends every request to the model the run was built with (start_run passes the
        # same name to --model and to the bridge), an upstream id taken as it is: nothing resolves
        # it to another model. The effort is the one the agent runs at.
        def flag(name: str) -> str:
            return str(argv[argv.index(name) + 1]) if name in argv[:-1] else ""
        return {"model": flag("--model"), "effort": flag("--effort")}

    def key_missing(self, cfg: dict | None = None) -> bool:
        # No key saved yet for the project's endpoint alias (an unreadable store says nothing): the
        # logout is a status line until one is saved, and the saved key's new stamp ends it (check_logins).
        from .. import endpoint
        from ..project import SecretsUnreadable
        s = ((cfg or {}).get("providers") or {}).get(self.name) if cfg is not None else self.entry
        alias = str(s.get("endpoint") or "") if isinstance(s, dict) else ""
        if not endpoint.ALIAS_RE.match(alias):
            return False
        try:
            return endpoint.credentials(alias) is None
        except SecretsUnreadable:
            return False

    account = Provider.account
    account_kind = Provider.account_kind


def fallback_cap_line(cfg: dict) -> str:
    """The doctor line for a project whose chat runs keep Claude Code's fallback-rate cap: a chat
    provider entry, billing auto or usage and no pricing.chat row. "" otherwise."""
    from ..billing import declared
    from ..chatbridge import price_ok
    if not isinstance((cfg.get("providers") or {}).get("chat"), dict) or declared(cfg, "chat") == "plan":
        return ""
    rows = (cfg.get("pricing") or {}).get("chat") if isinstance(cfg.get("pricing"), dict) else None
    if isinstance(rows, dict) and any(price_ok(v) for v in rows.values()):
        return ""
    return ("providers.chat: no pricing.chat row, so chat runs keep Claude Code's --max-budget-usd cap, "
            "priced at its fallback rate for a model it does not know: a guess that can cut finished work "
            "short. Set pricing.chat (\"default\": [input, cached input, output] $ per million tokens; "
            "zeros for a free endpoint) or providers.chat.billing to plan")
