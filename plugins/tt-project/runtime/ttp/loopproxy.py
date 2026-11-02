# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Proxy variables whose host is a literal `localhost` get 127.0.0.1 instead, for every worker.

Claude Code's Bash sandbox sets each command's proxy variables (HTTPS_PROXY, ALL_PROXY, ...) to its
own bridge on `localhost`, but the sandbox has an empty /etc/hosts and no DNS, so `localhost` does not
resolve there: `gh` and every other proxied tool fail with "error connecting to localhost" (measured,
CLI 2.1.296; the same call through 127.0.0.1 works). The sandbox sets these per command (bwrap
`--setenv`), after the worker's environment is built, and the CLI drops BASH_ENV, so the rewrite runs
inside each command: the CLI sources the file named by CLAUDE_ENV_FILE in every Bash command, after
the sandbox's variables are set (measured). That file is loopproxy.sh beside this module, the shell
twin of `loopback`; env_file() points CLAUDE_ENV_FILE at it and keeps an inherited one sourced after
it. The runner also rewrites the environment it starts any provider's agent with (fixed()), in memory
only: the values may carry the proxy's credentials, so they never go into run.json or a log.

The scheme, credentials, port and path are kept; other hosts, NO_PROXY and no_proxy are left alone."""
from __future__ import annotations

from pathlib import Path

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
              "FTP_PROXY", "ftp_proxy")
LOOPBACK = "127.0.0.1"
SCRIPT = Path(__file__).with_name("loopproxy.sh")
PREV_ENV = "TTP_PREV_CLAUDE_ENV_FILE"   # an inherited CLAUDE_ENV_FILE, sourced after the rewrite


def loopback(value: str) -> str:
    """`value` (a proxy URL or bare host:port) with a literal `localhost` host made 127.0.0.1; any other
    value unchanged. Mirrors loopproxy.sh step for step."""
    scheme, rest = "", value
    if "://" in value:
        head, rest = value.split("://", 1)
        scheme = head + "://"
    authority = rest.split("/", 1)[0]
    userinfo = authority.rsplit("@", 1)[0] + "@" if "@" in authority else ""
    host = rest[len(userinfo):]
    if host != "localhost" and not host.startswith(("localhost:", "localhost/")):
        return value
    return scheme + userinfo + LOOPBACK + host[len("localhost"):]


def fixed(env: dict) -> dict[str, str]:
    """The proxy variables of `env` that loopback() changes, with their new values."""
    out = {}
    for k in PROXY_VARS:
        v = env.get(k)
        if isinstance(v, str) and v and (new := loopback(v)) != v:
            out[k] = new
    return out


def env_file(environ: dict) -> dict[str, str]:
    """CLAUDE_ENV_FILE naming loopproxy.sh, plus PREV_ENV naming the CLAUDE_ENV_FILE `environ` already
    had (the script sources it after its own lines)."""
    prev = environ.get("CLAUDE_ENV_FILE") or ""
    out = {"CLAUDE_ENV_FILE": str(SCRIPT)}
    if prev and prev != str(SCRIPT):
        out[PREV_ENV] = prev
    return out
