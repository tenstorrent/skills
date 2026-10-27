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
