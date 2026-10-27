# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""MCP servers connected on this host that isolated Claude workers do not get.

`providers.claude.worker_isolation` starts workers with only the servers `providers.claude.mcp_servers`
lists, so a server the user connected with `claude mcp add` never reaches them, and a worker reports its
tools as unreadable with no hint why. The daemon runs `claude mcp list` (it probes every server, so it can
be slow) in the background, bounded by TIMEOUT_S, at most every TTL_S, from the project root (local and
project scope count too), and keeps the answer in the project's state. ttp status and the web app's feed
read only that file: a connected server neither listed nor withheld (`providers.claude.mcp_withheld`) is
one feed line. A failed or timed-out probe is unknown, never a warning. claude.ai connectors and servers
from a plugin are left out: `mcp_servers` cannot list them."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import boundedchild
from .project import write_json

CACHE = "mcp_list.json"
TTL_S = 6 * 3600       # a good answer is asked again this often
RETRY_S = 3600         # a failed or timed-out probe is tried again this soon
TIMEOUT_S = 90.0       # the whole `claude mcp list`, every server's probe included
CONNECTOR_PREFIXES = ("claude.ai", "claude_ai_")   # as providers.claude: account connectors
PLUGIN_PREFIX = "plugin:"


def parse(text: str) -> list[str]:
    """The names `claude mcp list` reports as connected: lines `<name>: <target> - <mark> Connected`."""
    out: list[str] = []
    for ln in text.splitlines():
        name, sep, rest = ln.strip().partition(": ")
        if not sep or " - " not in rest:
            continue
        status = rest.rsplit(" - ", 1)[1].strip().lower()
        if status.endswith("connected") and "not connected" not in status and name not in out:
            out.append(name)
    return out


def probe(binary: str, cwd: str, timeout: float = TIMEOUT_S, now: float | None = None) -> dict:
    """Run `claude mcp list` once; {"at", "ok", "connected"} or {"at", "ok": False, "error"}."""
    now = time.time() if now is None else now
    r = boundedchild.run([binary, "mcp", "list"], timeout, cwd=cwd, stderr="stdout")
    if r.rc != 0:
        why = r.error or f"exit {r.rc}: {' '.join(str(r.out).split())[-200:]}"
        return {"at": now, "ok": False, "error": why}
    return {"at": now, "ok": True, "connected": parse(str(r.out))}


def read(state: Path) -> dict | None:
    try:
        data = json.loads((Path(state) / CACHE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def due(entry: dict | None, now: float) -> bool:
    if not entry:
        return True
    return now - float(entry.get("at") or 0) >= (TTL_S if entry.get("ok") else RETRY_S)


def refresh(state: Path, binary: str, cwd: str, timeout: float = TIMEOUT_S) -> dict:
    entry = probe(binary, cwd, timeout)
    write_json(Path(state) / CACHE, entry)
    return entry


def _names(v) -> list[str]:
    from .coordinator import name_list
    return name_list(v or [])


def gap(claude_cfg: dict, entry: dict | None) -> dict:
    """{"state": "off" | "unknown" | "none" | "missing", "missing": [...], "line": str}; only
    "missing" has a line."""
    if not claude_cfg.get("worker_isolation"):
        return {"state": "off", "missing": [], "line": ""}
    if not entry or not entry.get("ok"):
        return {"state": "unknown", "missing": [], "line": "",
                "error": (entry or {}).get("error") or "not probed yet"}
    given = set(_names(claude_cfg.get("mcp_servers"))) | set(_names(claude_cfg.get("mcp_withheld")))
    missing = [n for n in entry.get("connected") or []
               if n not in given and not n.lower().startswith(CONNECTOR_PREFIXES) and not n.startswith(PLUGIN_PREFIX)]
    if not missing:
        return {"state": "none", "missing": [], "line": ""}
    return {"state": "missing", "missing": missing,
            "line": f"MCP servers connected on this host but not given to workers: {', '.join(missing)} "
                    "(add to providers.claude.mcp_servers, or to providers.claude.mcp_withheld to keep them out)"}


def line(state: Path, cfg: dict) -> str:
    """The feed line from the cached probe; "" when nothing is missing, isolation is off or unknown."""
    return gap((cfg.get("providers") or {}).get("claude") or {}, read(state))["line"]
