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

import os
from pathlib import Path

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
              "FTP_PROXY", "ftp_proxy")
JOB_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
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


def _loopback_host(value: str) -> bool:
    """Whether the proxy URL (or bare host:port) `value` names a loopback host."""
    import ipaddress
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(value if "://" in value else "http://" + value).hostname or "").lower()
    except ValueError:
        return False
    if host in LOOPBACK_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or bool(getattr(ip, "ipv4_mapped", None) and ip.ipv4_mapped.is_loopback)


def sandbox_only(env: dict) -> list[str]:
    """The proxy variables of `env` (HTTP(S)_PROXY, ALL_PROXY, either case) whose host is a loopback
    address: in a sandboxed command that is the sandbox's own bridge, which a job the daemon starts
    outside the sandbox cannot reach. Names only: the values may carry the proxy's credentials."""
    return [k for k in JOB_PROXY_VARS if isinstance(env.get(k), str) and env[k] and _loopback_host(env[k])]


def env_file(environ: dict) -> dict[str, str]:
    """CLAUDE_ENV_FILE naming loopproxy.sh, plus PREV_ENV naming the CLAUDE_ENV_FILE `environ` already
    had (the script sources it after its own lines). A daemon started from another project's run inherits
    that project's copy of this script and its PREV_ENV: a copy is never chained (it would source its
    PREV_ENV, itself, forever), only the file it chained to. PREV_ENV is always set, so an inherited one
    never survives."""
    prev = environ.get("CLAUDE_ENV_FILE") or ""
    if _copy(prev):
        prev = environ.get(PREV_ENV) or ""
    return {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: "" if _copy(prev) else prev}


def _copy(path: str) -> bool:
    """This script or any project's copy of it."""
    return bool(path) and os.path.basename(path) == SCRIPT.name


# Host loopback ports. The Linux Bash sandbox runs each command in its own network namespace (bwrap
# --unshare-net) whose only listeners are the proxy bridges, so a direct connect to a port on the host's
# loopback is refused there (measured, CLI 2.1.296), while the proxy itself reaches the host's
# loopback (a CONNECT to localhost:<port> answered 200). The CLI has no Linux setting that keeps the
# host's network. So the runner lists the host ports here (PORTS_ENV) and loopproxy.sh starts, inside
# each sandboxed command, a socat listener on 127.0.0.1:<port> per port that reaches the host's port
# through the proxy. NETNS_ENV names the worker's own namespace: the script forwards only from another
# one, so a command run outside the sandbox never binds a host port. macOS (Seatbelt, one namespace)
# gets no forwarders.
PORTS_ENV = "TTP_LOOPBACK_PORTS"
NETNS_ENV = "TTP_HOST_NETNS"
MAX_PORTS = 32   # each is one socat per sandboxed command
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "ip6-localhost"}
# /proc/net/tcp{,6} local addresses a connect to localhost reaches: loopback first, then every address.
_V4_LOOP, _V4_ANY = "0100007F", "00000000"
_V6_LOOP, _V6_ANY = "0" * 24 + "01000000", "0" * 32
_V6_MAPPED_LOOP, _V6_MAPPED_ANY = "0" * 16 + "FFFF0000" + _V4_LOOP, "0" * 16 + "FFFF0000" + _V4_ANY


def listening(net: Path = Path("/proc/net")) -> list[int]:
    """TCP ports listening on this host's 127.0.0.1 or ::1, then those listening on every address
    (also reachable at localhost); [] where /proc/net is missing."""
    loop, wild = [], []
    for name in ("tcp", "tcp6"):
        try:
            lines = (net / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) < 4 or f[3] != "0A":   # 0A: LISTEN
                continue
            addr, _, port = f[1].partition(":")
            addr = addr.upper()
            if addr in (_V4_LOOP, _V6_LOOP, _V6_MAPPED_LOOP):
                loop.append(int(port, 16))
            elif addr in (_V4_ANY, _V6_ANY, _V6_MAPPED_ANY):
                wild.append(int(port, 16))
    return list(dict.fromkeys(loop + wild))


def ssh_ports(path: Path | None = None) -> list[int]:
    """Ports of the host's ssh tunnel ends named in the user's ssh config: `Port` of a Host block whose
    HostName is a loopback name, and each LocalForward's listening port bound on loopback (or on no
    address). Include files are not read."""
    path = path or Path.home() / ".ssh" / "config"
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out: list[int] = []
    block: dict[str, str] = {}

    def flush():
        if block.get("hostname", "").lower() in LOOPBACK_NAMES and block.get("port", "").isdigit():
            out.append(int(block["port"]))
        block.clear()
    for line in lines:
        parts = line.strip().replace("=", " ", 1).split(None, 1)
        if not parts or parts[0].startswith("#"):
            continue
        key, value = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        if key in ("host", "match"):
            flush()
        elif key == "localforward":
            bind = value.split()[0] if value else ""
            host, _, port = bind.strip("[]").rpartition(":") if ":" in bind else ("", "", bind)
            if port.isdigit() and host.strip("[]").lower() in LOOPBACK_NAMES | {"", "*", "0.0.0.0"}:
                out.append(int(port))
        else:
            block.setdefault(key, value.split()[0] if value else "")
    flush()
    return out


def ports_env(setting=None, net: Path = Path("/proc/net"), ssh_config: Path | None = None) -> dict[str, str]:
    """The worker's PORTS_ENV and NETNS_ENV. `setting` is providers.<name>.loopback_ports: unset or a list
    of extra ports (forwarded first) adds the host's loopback listeners and its ssh tunnel ends; False
    forwards none, so sandboxed commands keep their own namespace with only the proxy."""
    if setting is False:
        return {}
    extra = [int(p) for p in (setting if isinstance(setting, list) else [])
             if isinstance(p, int) and not isinstance(p, bool)]
    ports = [p for p in dict.fromkeys([*extra, *ssh_ports(ssh_config), *listening(net)]) if 0 < p < 65536]
    try:
        netns = os.readlink("/proc/self/ns/net")
    except OSError:
        return {}   # no network namespaces here (macOS): nothing to forward from
    return {PORTS_ENV: " ".join(map(str, ports[:MAX_PORTS])), NETNS_ENV: netns} if ports else {}
