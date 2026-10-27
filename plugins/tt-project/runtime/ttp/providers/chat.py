# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Claude Code workers on an OpenAI-compatible (Chat Completions) endpoint, through the run's
loopback bridge (chatbridge.py). Off until a project configures `providers.chat` and routes work
to it; the key stays in the `ttp secret endpoint <alias>` store."""
from __future__ import annotations

from ..project import secrets_path
from . import register
from .base import Provider
from .claude import Claude


@register
class Chat(Claude):
    name = "chat"
    # The agent reaches only the loopback bridge; the runner's bridge reaches the upstream.
    api_host, api_base_env = "", ""
    login_hint = "save the endpoint's key with `ttp secret endpoint <alias> --url <base url>`"
    own_cache_breakpoints = 0

    # Claude Code prices a model name it does not know at its fallback rate. With pricing.chat rows
    # ({"<model id>" or "default": [input, cached input, output] in $ per million tokens}) a run is
    # repriced from its tokens and the runner enforces its budget on that figure; without them its
    # spend is Claude Code's figure, booked as estimated, and Claude Code's own cap stays.
    @property
    def caps_budget(self) -> bool:
        return not self.price()

    def price(self) -> tuple[float, float, float] | None:
        from ..chatbridge import price_ok
        rows = {k: v for k, v in (self.prices or {}).items() if price_ok(v)}
        row = rows.get(self.model) or rows.get("default")
        return tuple(float(x) for x in row) if row else None

    def build(self, *, budget_usd, **kw):
        return super().build(budget_usd=None if self.price() else budget_usd, **kw)

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

    def cost_so_far(self, output_path):
        return self.parse(output_path).cost_usd if self.price() else None

    def credential_files(self) -> list[str]:
        return [str(secrets_path())]   # a key saved again ends a logged-out pause

    def login_check(self) -> bool | None:
        return None   # the endpoint is asked by a run, through the bridge

    def resolved_session(self, argv, cwd, env=None):
        # The bridge sends every request to the model the run was built with (start_run passes the
        # same name to --model and to the bridge), an upstream id taken as it is: nothing resolves
        # it to another model. The effort is the one the agent runs at.
        def flag(name: str) -> str:
            return str(argv[argv.index(name) + 1]) if name in argv[:-1] else ""
        return {"model": flag("--model"), "effort": flag("--effort")}

    account = Provider.account
    account_kind = Provider.account_kind
