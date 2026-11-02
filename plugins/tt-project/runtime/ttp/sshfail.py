"""What a failed ssh to a project's host means, said from the user's side, without addresses.

`ttp listen`, `ttp say`, `ttp connect`, `ttp web` and every command forwarded to a project on
another machine explain an ssh failure (exit 255) through `kind` and `scrub`:

    ssh says                                            after a connection   before any
    -------------------------------------------------   ------------------   ----------
    could not resolve hostname, name or service not     network              network
      known, temporary failure in name resolution,
      network is unreachable, connection timed out,
      timeout (server not responding)
    closed by remote host, connection closed by,        host                 host
      connection reset, connection refused,
      kex_exchange_identification
    no route to host                                    host                 network
    anything else (permission denied, broken pipe, ...) other                network

`host`: the host answered and dropped or turned the connection away, so it is restarting or its ssh
server is down; this machine's network is fine. `network`: this machine has no path to it (or, on the
first try, nothing says otherwise). A listener whose outage began with the host dropping a working
connection keeps the `host` wording through later timeouts (a host down while it reboots times out);
only `offline` (no name lookup, network unreachable) switches it to `network`. An address ssh puts in its own text is replaced by the ssh alias
the user configured: user-facing text never names an IP.

`unresolved` tells one case apart: the resolver answered that the name does not exist (glibc's "name
or service not known", "no address associated with hostname"), so the route itself is broken (an ssh
alias with no Host entry, a mistyped name): a config error the project fixes, never a pause or a wait
for the network. "Temporary failure in name resolution" (no resolver reachable) stays an outage, and
so does macOS's "nodename nor servname provided", which an offline Mac also prints.
"""
from __future__ import annotations

import re

_NETWORK = re.compile(r"could not resolve hostname|name or service not known|temporary failure in name resolution"
                      r"|nodename nor servname|no address associated|network is unreachable|timed out"
                      r"|timeout, server not responding", re.I)
_HOST = re.compile(r"closed by remote host|connection closed by|connection reset|connection refused"
                   r"|kex_exchange_identification", re.I)
_NO_ROUTE = re.compile(r"no route to host", re.I)
# This machine lost its own path: no name lookup, no network. A timeout may just be the host down.
_OFFLINE = re.compile(r"could not resolve hostname|name or service not known|temporary failure in name resolution"
                      r"|nodename nor servname|no address associated|network is unreachable", re.I)

# The resolver's answer that the name does not exist (module doc): group 1 is the name.
_NO_SUCH_NAME = re.compile(r"could not resolve hostname ([^\s:]+): (?:name or service not known"
                           r"|no address associated with hostname)", re.I)

_IPV4 = r"\d{1,3}(?:\.\d{1,3}){3}"
_V4 = re.compile(rf"(?<![\w.]){_IPV4}(?![\w.])")
# Hex groups with at least two colons, an optional IPv4 tail and zone; kept only when it is an address
# (it has `::`, or seven colons), so a time like 14:23:05 stays.
_V6 = re.compile(rf"(?<![\w:])(?:[0-9A-Fa-f]{{0,4}}:){{2,7}}(?:{_IPV4}|[0-9A-Fa-f]{{0,4}})(?:%[\w.]+)?(?![\w:])")
# ssh's own lines; the far side's command output is not touched.
_SSH_LINE = re.compile(r"^(?:ssh:|Connection to |Connection (?:closed|reset) by|kex_exchange_identification|"
                       r"client_loop:|Timeout, server|Warning: Permanently added)")


def kind(stderr: str | None, connected: bool = False) -> str:
    """'host', 'network' or 'other' (see the module's table). `connected`: this command (or listener)
    already reached the host once."""
    text = stderr or ""
    if _NETWORK.search(text):
        return "network"
    if _HOST.search(text):
        return "host"
    if _NO_ROUTE.search(text):
        return "host" if connected else "network"
    return "other" if connected else "network"


def unresolved(stderr: str | None) -> str:
    """The name ssh's error says does not exist (a broken route, module doc), else ""."""
    m = _NO_SUCH_NAME.search(stderr or "")
    return m.group(1) if m else ""


def offline(stderr: str | None) -> bool:
    """ssh's error says this machine has no network or cannot look names up (unlike a timeout, which
    a host that is down also gives)."""
    return bool(_OFFLINE.search(stderr or ""))


def no_ips(text: str, alias: str) -> str:
    """`text` with every IPv4 and IPv6 address replaced by `alias`."""
    def v6(m: re.Match) -> str:
        s = m.group(0)
        return alias if "::" in s or s.count(":") >= 7 else s
    return _V4.sub(alias, _V6.sub(v6, text))


def scrub(stderr: str | None, alias: str) -> str:
    """stderr with the addresses in ssh's own lines replaced by `alias`."""
    return "".join(no_ips(ln, alias) if _SSH_LINE.match(ln) else ln
                   for ln in (stderr or "").splitlines(keepends=True))


def why(stderr: str | None, alias: str, rc: int = 255) -> str:
    """ssh's last line, addresses replaced, for a message in brackets."""
    last = ((stderr or "").strip().splitlines() or [f"ssh exit {rc}" if rc != 255 else "unknown ssh error"])[-1]
    return no_ips(last, alias)
