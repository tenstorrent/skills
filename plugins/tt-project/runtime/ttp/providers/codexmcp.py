# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Codex workers' MCP servers (`providers.codex.mcp_servers`), as Claude's isolated workers get theirs.

Codex has no per-run MCP config file, so the listed servers go in as one `-c mcp_servers={...}`
inline TOML table (measured on codex-cli 0.160): a `-c` table merges into the user's config.toml
servers, and a dotted `-c mcp_servers.<name>.*` key cannot name a server whose name holds a dot or
a space. Each listed server comes from the project's `.mcp.json` (the file Claude reads), with
`${VAR}` and `${VAR:-default}` in its env and headers
expanded as Claude does. A worker that still reads the user's
config.toml (no worker_isolation, or no --ignore-user-config) keeps a listed server defined there
as it is, and every other server it defines gets `enabled=false` (a `-c` entry for a server that
exists nowhere is a startup error, so only defined ones are turned off).

Secrets never go in the arguments: a stdio server's env values go in the worker's environment and
`env_vars` forwards them; an HTTP server's `Authorization: Bearer` header becomes
`bearer_token_env_var` and its other headers `env_http_headers`, each naming a variable that holds
the value (`${VAR}` alone names VAR itself). The daemon hands those values to the runner in an
owner-only file, never run.json. `command`, `args` and `url` go in as written, so a server whose
`command`, `args` or `url` names a variable is not given (its value would be on the command line). Codex runs stdio and streamable HTTP servers only (no `sse`)."""
from __future__ import annotations

import importlib
import json
import os
import re
from pathlib import Path

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
HTTP_TYPES = ("http", "streamable-http", "streamable_http")
BEARER_RE = re.compile(r"\s*Bearer\s+(.*\S)\s*$", re.I)


# Variables the daemon sets in every worker's environment once its servers are chosen (start_run).
HARNESS_VARS = ("PATH", "PYTHONPATH", "TMPDIR", "TMP", "TEMP", "VIRTUAL_ENV")
HARNESS_PREFIXES = ("TTP_", "GIT_CONFIG_")


def overridden(env: dict[str, str], inherited) -> str | None:
    """The first variable of a server's environment `env` that would change the worker's own: one
    `inherited` (the worker's environment) holds with another value, or one the harness sets (the
    device mask included) to a value `inherited` does not already hold. A stdio server's env is the
    whole worker's, as Codex starts the server from it."""
    from ..devq import DEVICE_MASK, MASK_RECORD
    for k, v in env.items():
        ours = k.startswith("TTP_MCP_")   # a name translate made for a header's value
        harness = not ours and (k in HARNESS_VARS or k.startswith(HARNESS_PREFIXES)
                                or k in DEVICE_MASK or k == MASK_RECORD)
        if inherited.get(k, v) != v or (harness and inherited.get(k) != v):
            return k
    return None


class Unset(ValueError):
    """A `${VAR}` with no default names a variable that is not set."""


def expand(text: str, environ) -> str:
    def one(m):
        if m.group(1) in environ:
            return environ[m.group(1)]
        if m.group(2) is not None:
            return m.group(2)
        raise Unset(m.group(1))
    return VAR_RE.sub(one, str(text))


def only_var(text) -> str | None:
    """VAR when `text` is exactly `${VAR}`, else None."""
    m = VAR_RE.fullmatch(str(text).strip())
    return m.group(1) if m and m.group(2) is None else None


def toml_value(v) -> str:
    from .codex import toml_string
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{toml_string(str(k))}={toml_value(x)}" for k, x in v.items()) + "}"
    return toml_string(str(v))


def project_servers(dirs: list[str]) -> dict:
    """`.mcp.json` servers for the directories `dirs`, the first file to define a name winning."""
    out: dict = {}
    for d in dirs:
        try:
            servers = json.loads((Path(d) / ".mcp.json").read_text()).get("mcpServers")
        except (OSError, ValueError, AttributeError):
            continue
        for name, entry in (servers or {}).items() if isinstance(servers, dict) else ():
            if isinstance(entry, dict):
                out.setdefault(name, entry)
    return out


def user_server_names(config_toml: str) -> list[str]:
    """The MCP servers a Codex config.toml defines: its `[mcp_servers.<name>]` tables and the keys
    of a `[mcp_servers]` table (tomllib where Python has it, 3.11+; loaded by name, since the runtime
    supports 3.9)."""
    try:
        tomllib = importlib.import_module("tomllib")
        got = tomllib.loads(config_toml).get("mcp_servers")
        return [n for n, v in got.items() if isinstance(v, dict)] if isinstance(got, dict) else []
    except ImportError:
        pass
    except ValueError:
        return []   # Codex itself refuses to start on a broken config.toml
    key = r'"(?:[^"\\]|\\.)*"|\'[^\']*\'|[A-Za-z0-9_-]+'
    names, section = [], None
    for line in config_toml.splitlines():
        if m := re.match(r"\s*\[\[?\s*([^\]]*?)\s*\]", line):
            section = m.group(1)
            if h := re.match(rf"mcp_servers\s*\.\s*({key})", section):
                names.append(_unquote(h.group(1)))
        elif section == "mcp_servers" and (k := re.match(rf"\s*({key})\s*[.=]", line)):
            names.append(_unquote(k.group(1)))
        elif section is None and (k := re.match(rf"\s*mcp_servers\s*\.\s*({key})\s*[.=]", line)):
            names.append(_unquote(k.group(1)))
    return list(dict.fromkeys(names))


def _unquote(key: str) -> str:
    if key.startswith('"'):
        try:
            return json.loads(key)
        except ValueError:
            return key[1:-1]
    return key[1:-1] if key.startswith("'") else key


def _slug(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "SERVER"


def plain(field: str, text) -> str:
    """`text` for the command line; ValueError if it names a variable, whose value would go there."""
    if m := VAR_RE.search(str(text)):
        raise ValueError(f"its {field} names ${{{m.group(1)}}}, whose value would go on the command line")
    return str(text)


def translate(name: str, entry: dict, environ) -> tuple[dict, dict[str, str]]:
    """The Codex table for one `.mcp.json` entry, and the secret environment it reads. Raises
    ValueError (Unset included) for an entry Codex cannot run."""
    kind = str(entry.get("type") or ("http" if entry.get("url") else "stdio")).lower()
    secrets: dict[str, str] = {}
    if kind == "stdio":
        if not isinstance(entry.get("command"), str) or not entry["command"].strip():
            raise ValueError("no command")
        if not isinstance(entry.get("args") or [], list):
            raise ValueError("args is not a list")
        table: dict = {"command": plain("command", entry["command"]),
                       "args": [plain("args", a) for a in entry.get("args") or []]}
        env = entry.get("env") or {}
        for k, v in env.items() if isinstance(env, dict) else ():
            secrets[str(k)] = expand(v, environ)
        if secrets:
            table["env_vars"] = list(secrets)
        return table, secrets
    if kind not in HTTP_TYPES:
        raise ValueError(f"type {kind}: Codex runs stdio and streamable HTTP servers only")
    if not entry.get("url"):
        raise ValueError("no url")
    table = {"url": plain("url", entry["url"])}
    headers = entry.get("headers") or {}
    for i, (h, v) in enumerate(headers.items() if isinstance(headers, dict) else ()):
        bearer = BEARER_RE.match(str(v)) if str(h).lower() == "authorization" else None
        value = bearer.group(1) if bearer else v
        var = only_var(value)
        if var is None:
            var = f"TTP_MCP_{_slug(name)}_" + ("TOKEN" if bearer else f"HEADER{i}_TOKEN")
            secrets[var] = expand(value, environ)
        elif var not in environ:
            raise Unset(var)
        if bearer:
            table["bearer_token_env_var"] = var
        else:
            table.setdefault("env_http_headers", {})[str(h)] = var
    return table, secrets


def run_args(names: list[str], dirs: list[str], user_names: list[str], isolated: bool,
             environ=None, inherited=None) -> tuple[list[str], dict[str, str], list[str]]:
    """(`-c` arguments, secret environment, names Codex will not run, each with why) for a worker
    that gets exactly the servers `names`. `user_names`: the servers the user's config.toml
    defines, which load unless `isolated`. `environ` expands `${VAR}`; `inherited` is the worker's
    environment (default `environ`), which no server may change."""
    if not names:
        return [], {}, []
    environ = os.environ if environ is None else environ
    inherited = environ if inherited is None else inherited
    project = project_servers(dirs)
    tables: dict = {}
    secrets: dict[str, str] = {}
    unknown: list[str] = []
    for name in names:
        if not isolated and name in user_names:
            tables[name] = {"enabled": True}
        elif name in project:
            try:
                table, env = translate(name, project[name], environ)
            except Unset as e:
                unknown.append(f"{name} (${{{e}}} is not set)")
                continue
            except ValueError as e:
                unknown.append(f"{name} ({e})")
                continue
            if key := overridden(env, inherited):
                unknown.append(f"{name} (its env {key} would change the worker's own)")
                continue
            if clash := [k for k in env if secrets.get(k, env[k]) != env[k]]:
                unknown.append(f"{name} (its {clash[0]} differs from another listed server's)")
                continue   # one worker environment holds one value
            tables[name] = {**table, "enabled": True}
            secrets.update(env)
        else:
            unknown.append(name)
    if not isolated:
        tables.update({n: {"enabled": False} for n in user_names if n not in names})
    return (["-c", "mcp_servers=" + toml_value(tables)] if tables else []), secrets, unknown


def parse_list(text: str) -> dict[str, bool]:
    """`codex mcp list --json` as {server name: enabled}; {} for output it cannot read."""
    try:
        rows = json.loads(text)
    except ValueError:
        return {}
    return {str(r["name"]): bool(r.get("enabled", True)) for r in rows if isinstance(r, dict) and "name" in r} \
        if isinstance(rows, list) else {}
