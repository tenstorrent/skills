# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""MCP servers connected on this host that isolated Claude workers do not get.

`providers.claude.worker_isolation` starts workers with only the servers `providers.claude.mcp_servers`
lists, so a server the user connected with `claude mcp add` never reaches them, and a worker reports its
tools as unreadable with no hint why. The daemon runs `claude mcp list` (it probes every server, so it can
be slow) in the background, bounded by TIMEOUT_S, at most every TTL_S, from the project root (local and
project scope count too), and keeps the answer in the project's state. ttp status and the web app's feed
read only that file: a connected server neither listed nor withheld (`providers.claude.mcp_withheld`) is
one feed line. A failed or timed-out probe is unknown, never a warning. Servers from a plugin are left
out: `mcp_servers` cannot list them. Connected claude.ai account connectors get a second line unless
listed or withheld: `--strict-mcp-config` drops every server not passed in `--mcp-config`, and a
connector is no file-defined server, so it cannot be passed on.

A connector listed in `mcp_servers` reaches isolated workers another way (plan()): the run leaves out
--strict-mcp-config and keeps out by name every other server this host knows (every name the last good
probe reported, connected or not, and the Claude config scopes for the run's directories). That needs a
list to trust: none yet, one older than TRUST_S, or a plugin dir that brings servers of its own keeps the
run strict, and the reason is a feed line, a doctor line and a low alert."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import boundedchild
from .project import write_json

CACHE = "mcp_list.json"
TTL_S = 6 * 3600       # a good answer is asked again this often
RETRY_S = 3600         # a failed or timed-out probe is tried again this soon
TIMEOUT_S = 90.0       # the whole `claude mcp list`, every server's probe included
TRUST_S = 2 * TTL_S    # an older list no longer lets a run keep connectors (two missed refreshes)
CONNECTOR_PREFIXES = ("claude.ai", "claude_ai_")   # as providers.claude: account connectors
PLUGIN_PREFIX = "plugin:"


def states(text: str) -> dict[str, str]:
    """Each server `claude mcp list` reports, by name: "connected", "needs_auth", or "failed" (any other mark,
    such as a connected server whose tools could not be read). Lines `<name>: <target> - <mark> <status>`."""
    out: dict[str, str] = {}
    for ln in text.splitlines():
        name, sep, rest = ln.strip().partition(": ")
        if not sep or " - " not in rest or name in out:
            continue
        status = rest.rsplit(" - ", 1)[1].strip().lower()
        out[name] = ("connected" if status.endswith("connected") and "not connected" not in status
                     else "needs_auth" if "needs auth" in status else "failed")
    return out


def parse(text: str) -> list[str]:
    """The names `claude mcp list` reports as connected."""
    return [n for n, s in states(text).items() if s == "connected"]


def listed(text: str) -> list[str]:
    """Every server name `claude mcp list` reports, connected or not."""
    out: list[str] = []
    for ln in text.splitlines():
        name, sep, rest = ln.strip().partition(": ")
        if sep and " - " in rest and name not in out:
            out.append(name)
    return out


def split(names: list[str]) -> tuple[list[str], list[str]]:
    """parse()'s names as (servers, connectors): claude.ai account connectors apart, plugin servers in neither."""
    connectors = [n for n in names if n.lower().startswith(CONNECTOR_PREFIXES)]
    return [n for n in names if n not in connectors and not n.startswith(PLUGIN_PREFIX)], connectors


def probe(binary: str, cwd: str, timeout: float = TIMEOUT_S, now: float | None = None) -> dict:
    """Run `claude mcp list` once; {"at", "ok", "connected"} or {"at", "ok": False, "error"}."""
    now = time.time() if now is None else now
    r = boundedchild.run([binary, "mcp", "list"], timeout, cwd=cwd, stderr="stdout")
    if r.rc != 0:
        why = r.error or f"exit {r.rc}: {' '.join(str(r.out).split())[-200:]}"
        return {"at": now, "ok": False, "error": why}
    return {"at": now, "ok": True, "connected": parse(str(r.out)), "names": listed(str(r.out))}


def read(state: Path) -> dict | None:
    try:
        data = json.loads((Path(state) / CACHE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def due(entry: dict | None, now: float) -> bool:
    if not entry or (entry.get("ok") and "names" not in entry):   # a list from before plan(): ask again
        return True
    return now - float(entry.get("at") or 0) >= (TTL_S if entry.get("ok") else RETRY_S)


def refresh(state: Path, binary: str, cwd: str, timeout: float = TIMEOUT_S) -> dict:
    entry = probe(binary, cwd, timeout)
    if not entry.get("ok"):   # a failed probe keeps the last good list (plan() judges its age)
        prev = read(state) or {}
        good = prev if prev.get("ok") else prev.get("last_ok")
        if isinstance(good, dict) and "names" in good:
            entry["last_ok"] = {k: good[k] for k in ("at", "connected", "names") if k in good}
    write_json(Path(state) / CACHE, entry)
    return entry


def _names(v) -> list[str]:
    from .coordinator import name_list
    return name_list(v or [])


def tool_name(name: str) -> str:
    """The server part of its tools' names, mcp__<this>__<tool>, as Claude Code writes it: every
    character but letters, digits, _ and - becomes _, and a claude.ai connector's runs of _ collapse."""
    out = re.sub(r"[^A-Za-z0-9_-]", "_", str(name))
    return re.sub(r"_+", "_", out).strip("_") if _is_connector(name) else out


def _is_connector(name: str) -> bool:
    from .providers.claude import CONNECTOR_PREFIXES as prefixes
    return str(name).lower().startswith(prefixes)


def _key(name: str) -> str:
    """One connector however it is written: "claude.ai Gmail", "claude_ai_Gmail", "mcp__claude_ai_Gmail"."""
    name = str(name)
    return tool_name(name[5:] if name.startswith("mcp__") else name).lower()


def host_list(entry: dict | None, now: float | None = None) -> tuple[list[str] | None, str]:
    """Every server name the last good probe reported, or (None, why) when there is none to trust."""
    now = time.time() if now is None else now
    entry = entry or {}
    good = entry if entry.get("ok") else entry.get("last_ok")
    if not isinstance(good, dict) or not isinstance(good.get("names"), list):
        why = entry.get("error") if entry and not entry.get("ok") else ""
        return None, "no list of this host's MCP servers yet" + (f" (`claude mcp list`: {why})" if why else "")
    age = now - float(good.get("at") or 0)
    if age < -300:
        return None, "the last list of this host's MCP servers is dated in the future (clock change?)"
    if age > TRUST_S:
        return None, f"the last list of this host's MCP servers is {age / 3600:.0f} h old"
    return [str(n) for n in good["names"]], ""


def plugin_servers(dirs: list[str]) -> list[str]:
    """The plugin dirs whose plugin brings MCP servers of its own (they are on no list to keep out)."""
    out = []
    for d in dirs:
        try:
            manifest = json.loads((Path(d) / ".claude-plugin" / "plugin.json").read_text())
        except (OSError, ValueError):
            manifest = {}
        if (Path(d) / ".mcp.json").exists() or (isinstance(manifest, dict) and manifest.get("mcpServers")):
            out.append(d)
    return out


def plan(claude_cfg: dict, entry: dict | None, scope: list[str] = (), plugin_dirs: list[str] = (),
         now: float | None = None) -> dict:
    """How an isolated Claude run treats MCP servers: {"connectors": [...], "deny": [...], "why": str}.
    No connector listed in mcp_servers: all empty, the run stays strict as it always was. A listed one
    on this host's trusted list: the run keeps those connectors, and every other name on the list or in
    `scope` (the config scopes' servers) but the listed file servers goes in "deny". Else "why" says why
    the run stays strict: a run never leaves strict mode with an unknown set of servers. A connector
    also on mcp_withheld is never kept (chat inherits claude's withheld list: project.CHAT_INHERITS)."""
    names = _names(claude_cfg.get("mcp_servers"))
    withheld = {_key(n) for n in _names(claude_cfg.get("mcp_withheld"))}
    wanted = [n for n in names if _is_connector(n) and _key(n) not in withheld]
    out = {"connectors": [], "deny": [], "why": ""}
    if not wanted:
        return out
    host, why = host_list(entry, now)
    if host is None:
        return {**out, "why": why}
    brings = plugin_servers(list(plugin_dirs))
    if brings:
        return {**out, "why": f"plugin dirs bring MCP servers of their own: {', '.join(brings)}"}
    keys = {_key(n) for n in wanted}
    keep = [n for n in host if _is_connector(n) and _key(n) in keys]
    if not keep:
        return {**out, "why": f"{', '.join(wanted)} not on this host's list of MCP servers"}
    files = {n for n in names if not _is_connector(n)}
    deny = [n for n in dict.fromkeys([*host, *scope]) if n not in keep and n not in files]
    return {"connectors": keep, "deny": deny, "why": ""}


def gap(claude_cfg: dict, entry: dict | None) -> dict:
    """{"state": "off" | "unknown" | "none" | "missing", "missing": [...], "connectors": [...], "undecided": [...],
    "line": str, "detail": str}. "undecided" names each connected server or connector in neither mcp_servers nor
    mcp_withheld (the coordinator decides them, mcp_gap events); "line" names them in one short line, ""
    once every one is decided; "detail" is the full text for `ttp status --verbose`."""
    if not claude_cfg.get("worker_isolation"):
        return {"state": "off", "missing": [], "connectors": [], "undecided": [], "line": "", "detail": ""}
    if not entry or not entry.get("ok"):
        return {"state": "unknown", "missing": [], "connectors": [], "undecided": [], "line": "", "detail": "",
                "error": (entry or {}).get("error") or "not probed yet"}
    listed, withheld = set(_names(claude_cfg.get("mcp_servers"))), set(_names(claude_cfg.get("mcp_withheld")))
    servers, found = split(entry.get("connected") or [])
    missing = [n for n in servers if n not in listed | withheld]
    # A listed connector reaches isolated workers while plan() trusts the list; withheld ones are not named.
    keys = {_key(n) for n in listed}
    why = plan(claude_cfg, entry)["why"]
    connectors = [n for n in found if n not in withheld and (why or _key(n) not in keys)]
    unlisted = [n for n in connectors if _key(n) not in keys]
    undecided = missing + unlisted
    given = [n for n in servers + found if n not in withheld and n not in undecided]
    kept = [n for n in servers + found if n in withheld]
    lines = []
    if missing:
        lines.append(f"MCP servers connected on this host but not given to workers: {', '.join(missing)} "
                     "(add to providers.claude.mcp_servers, or to providers.claude.mcp_withheld to keep them out)")
    if unlisted:
        lines.append(f"Connected claude.ai connectors ({', '.join(unlisted)}) are not given to isolated workers. "
                     "List one in providers.claude.mcp_servers to give it to them (every other server stays out), "
                     "or add them to providers.claude.mcp_withheld to keep them out")
    stuck = [n for n in connectors if _key(n) in keys]
    if stuck:
        lines.append(f"claude.ai connectors listed in providers.claude.mcp_servers ({', '.join(stuck)}) do not "
                     f"reach isolated workers: {why}; they run with --strict-mcp-config until that clears")
    lines.append(f"MCP servers connected on this host: for workers {', '.join(given) or 'none'}; "
                 f"withheld {', '.join(kept) or 'none'}")
    short = (f"MCP servers not yet decided for workers: {', '.join(undecided)} (the coordinator decides)"
             if undecided else "")
    return {"state": "missing" if missing or connectors else "none", "missing": missing, "connectors": connectors,
            "undecided": undecided, "line": short, "detail": "\n".join(lines)}


def line(state: Path, cfg: dict, verbose: bool = False) -> str:
    """The feed line from the cached probe: the undecided names, "" once each is decided, isolation is off or
    the probe unknown; verbose: the full text, decided names too."""
    return gap((cfg.get("providers") or {}).get("claude") or {}, read(state))["detail" if verbose else "line"]


DECIDED_KV = "mcp_gap_raised"   # every name an mcp_gap event already named


def raise_undecided(db, state: Path, cfg: dict, now: float, name: str = "<project>") -> list[str]:
    """Queue one mcp_gap coordinator event naming the undecided servers and connectors no event named yet.
    A name connected and decided leaves the record, so one undecided again later is raised again. Where the
    user set providers.claude.mcp_withheld or mcp_servers (configwho), the coordinator may not change it: the
    event says the names are already out of isolated workers and gives the user's `ttp config` command."""
    entry = read(state)
    names = gap((cfg.get("providers") or {}).get("claude") or {}, entry)["undecided"]
    raised = db.kv(DECIDED_KV)
    raised = [str(n) for n in raised] if isinstance(raised, list) else []   # a broken row: raise again, once
    connected = set((entry or {}).get("connected") or []) if (entry or {}).get("ok") else set()
    keep = [n for n in raised if n in names or n not in connected]
    new = [n for n in names if n not in raised]
    if not new:
        if keep != raised:
            db.set_kv(DECIDED_KV, keep)
        return []
    from . import configwho
    claude = (cfg.get("providers") or {}).get("claude") or {}
    owned = [k for k in ("mcp_withheld", "mcp_servers")
             if configwho.role(configwho.get(db, f"providers.claude.{k}")) == "user"]
    text = (f"MCP servers or claude.ai connectors connected on this host that isolated workers have no decision "
            f"for: {', '.join(new)}. ")
    if owned:   # the user's value: a coordinator config_set of it is refused, so never ask for one
        withheld = [str(n) for n in claude.get("mcp_withheld") or []]
        whole = json.dumps([*withheld, *(n for n in new if n not in withheld)])
        text += (f"providers.claude.{' and '.join(owned)} {'is' if len(owned) == 1 else 'are'} the user's setting, "
                 "so do not config_set it. Isolated workers run with --strict-mcp-config, so an undecided server or "
                 "connector is already kept out of them: nothing leaks. To record the decision the user runs "
                 f"`ttp config {name} providers.claude.mcp_withheld '{whole}'`.")
    else:
        text += ("Decide each one yourself, not an ask: config_set providers.claude.mcp_servers (gives it to workers) "
                 "or providers.claude.mcp_withheld (keeps it out), each the whole list with the names already there. "
                 "Withhold when unsure; listing a connector in mcp_servers keeps every other server out.")
    with db.tx():   # the event and the record of its names commit together
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, "harness", "mcp_gap", f"mcp_gap:{'|'.join(sorted(new))}", "normal", text, "queued"))
        db.set_kv(DECIDED_KV, [*keep, *new])
    return new
