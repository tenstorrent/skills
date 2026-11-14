# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A run never exposes its own model credentials or proxy outside the run.

A run's model reaches it through a per-run token and, on the chat provider, a loopback proxy of the
runner (ANTHROPIC_BASE_URL, and TTP_RUN_PROXY_PORT for its port). Both belong to the run: whatever reaches the
proxy with the token is served by the run's own model, and a container or another agent holding the token acts
as the run. hook.py refuses, on every provider and for every task (model-free, always on), a Bash call that:
- hands the run's credential variables (CRED_VARS) or the token's value to a container or another agent:
  docker, podman, nerdctl, apptainer, singularity, docker-compose or harbor naming `$VAR`, `${VAR}` or the
  bare name (`-e VAR`, `-eVAR`, `--ae VAR`, which pass the current value) or the token's value, or an env file
  (`.env`, `*.env`, `env-file` ...) written with one, or a whole-environment dump (`env`, `printenv`,
  `export -p`, `set`, `declare -x/-p`) written to a file or process substitution in the same command that runs a
  container tool with `--env-file`. A literal value (`-e VAR=x`, `-eVAR=x`) is no credential;
- opens a listener on a non-loopback address (no `bind=` at all counts: it listens on every address) that
  forwards to the run's proxy port: socat `*-LISTEN:` with a non-loopback or missing `bind=`, `ssh -L/-R` (and
  autossh, `-o LocalForward/RemoteForward=`) with a non-loopback bind or with the bind widened (`-g`,
  `-o GatewayPorts=yes|clientspecified`; `GatewayPorts=no` is fine), `nc`/`ncat -l` without a loopback bind,
  or inline code of python, node, perl or ruby binding a non-loopback address beside the port;
- opens the firewall for that port (iptables, ip6tables, nft, ufw or firewall-cmd naming it, but a DROP or
  REJECT rule or a delete).
It is no sudo block and no sandbox (unsandboxed runs stay): sudo, docker and every listener on loopback pass.
The messages name the safe route, `ttp secret env <name> -- <command>` for a stored secret.

The proxy port comes from the runner (TTP_RUN_PROXY_PORT, set where it starts the bridge) and, for any
provider, from a loopback `*_BASE_URL` in the run's environment. Known gaps, accepted: a script file a command
runs is not read; a `--env-file` or env dump another call wrote (only the same command is judged); a listener and
its target in separate calls (or a forward whose port comes from a variable or is built at run time); `docker run
--network host`; obfuscated or encoded forms (base64, a value or name assembled in pieces, `eval`); a credential
read from a file or `/proc/<pid>/environ` and passed on; a token handed over by a means that names neither a
credential variable nor its value (stdin, a mounted file). A command that cannot be parsed passes.
"""
from __future__ import annotations

import os
import re

# The run's own model credentials: the variables the runner and the provider CLIs hand the agent
CRED_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN")
# Commands that start a container or another agent with an environment of its own
CONTAINER_TOOLS = {"docker", "podman", "nerdctl", "apptainer", "singularity", "docker-compose", "podman-compose",
                   "harbor"}
FIREWALL_TOOLS = {"iptables", "ip6tables", "nft", "ufw", "firewall-cmd", "iptables-nft", "iptables-legacy"}
NC_TOOLS = {"nc", "ncat", "netcat", "nc.openbsd", "nc.traditional"}
GUARDED_TOOLS = FIREWALL_TOOLS | NC_TOOLS | {"socat", "socat1", "ssh", "autossh"}
INTERPRETERS_RE = re.compile(r"^(?:python[\d.]*|node(?:js)?|perl|ruby)$")
LOOPBACK_RE = re.compile(r"^(?:localhost|127(?:\.\d{1,3}){3}|::1|\[::1\]|ip6-localhost)$", re.I)
# A file an env is written to
ENV_FILE_RE = re.compile(r"(?:>>?|\btee(?:\s+-a)?)\s*[\"']?(?:[^\s;&|\"']*/)?"
                         r"(?:\.env[\w.-]*|[\w.-]*\.env|[\w.-]*[_-]env|env[._-]?file[\w.-]*|env\.list)[\"']?(?=[\s;&|]|$)", re.I)

# A dump of the whole environment (env, printenv, export -p, set, declare -x/-p) written to a file or a pipe
# into tee, or inside a process or command substitution
ENV_DUMP_RE = re.compile(r"(?<![\w./$-])(?:env|printenv|export\s+-p|set|declare\s+-[xp])\s*(?:>>?|\|\s*tee\b|\))")
# ssh options that widen a forward's bind: -g, -o GatewayPorts=yes|clientspecified
SSH_GATEWAY_RE = re.compile(r"(?i)GatewayPorts[ =]+\"?(?:yes|clientspecified)\b")
SSH_G_FLAG_RE = re.compile(r"^-[46AaCfGKkMNnqsTtVvXxYy]*g[46AaCfGKkMNnqsTtVvXxYy]*$")

CRED_WHY = ("a run must not hand its own model credentials ({names}) to a container or another agent: "
            "whoever holds the token acts as this run. Give a command a stored secret with "
            "`ttp secret env <name> -- <command>` (a secret stored with `ttp secret set` or `generate`: "
            "`ttp secret env --secret <name>[:<key>]=<VAR> -- <command>`); a literal value "
            "(`-e FOO=bar`) is fine. Not the run's variable, not an env file made from it")
PROXY_WHY = ("a run must not expose its own model proxy (port {port}, with the run's token it serves the run's "
             "own model): no listener on a non-loopback address forwarding to it and no firewall rule "
             "opening it. A forwarder bound to loopback (`bind=127.0.0.1`, `ssh -L 127.0.0.1:...`) is fine; "
             "give a container or another agent a model with `ttp secret env <name> -- <command>`")


def proxy_ports(env: dict | None = None) -> set[str]:
    """The run's own proxy ports: TTP_RUN_PROXY_PORT (comma list) and the port of each loopback `*_BASE_URL`."""
    env = os.environ if env is None else env
    ports = {p.strip() for p in str(env.get("TTP_RUN_PROXY_PORT") or "").split(",") if p.strip().isdigit()}
    for k, v in env.items():
        if k.endswith("_BASE_URL") and (m := re.match(r"^\w+://(?:localhost|127(?:\.\d{1,3}){3}|\[::1\]):(\d+)(?:[/?]|$)", str(v))):
            ports.add(m[1])
    return ports


def _base(w: str) -> str:
    return os.path.basename(w)


def _has_port(text: str, port: str) -> bool:
    return bool(re.search(rf"(?<![\d.]){re.escape(port)}(?!\d)", text))


def _loopback(addr: str) -> bool:
    return bool(LOOPBACK_RE.match(addr.strip().strip("[]") if addr.strip() != "[::1]" else addr.strip()))


def _split_spec(spec: str) -> list[str]:
    """A forward spec's fields split at ':' outside [] (IPv6 addresses)."""
    return re.findall(r"\[[^\]]*\]|[^:]+|(?<=:)(?=:)|^(?=:)", spec) if spec else []


def _forward_bind(spec: str) -> tuple[str | None, str] | None:
    """(bind, destination port) of an ssh forward spec `[bind:]port:host:hostport`; bind None when absent
    (ssh then binds loopback), "" or `*` when given empty (every address)."""
    parts = spec.split(":") if "[" not in spec else _split_spec(spec)
    if len(parts) == 4:
        return parts[0], parts[3]
    if len(parts) == 3:
        return None, parts[2]
    return None


def _ssh_forwards(argv: list[str]) -> list[str]:
    out = []
    for i, w in enumerate(argv):
        if w in ("-L", "-R") and i + 1 < len(argv):
            out.append(argv[i + 1])
        elif re.match(r"^-[LR]\S", w):
            out.append(w[2:])
        elif w in ("-o",) and i + 1 < len(argv) and re.match(r"(?i)^(?:Local|Remote)Forward[ =]", argv[i + 1]):
            out.append(re.split(r"[ =]+", argv[i + 1], 1)[1].replace(" ", ":"))
        elif re.match(r"(?i)^-o(?:Local|Remote)Forward[ =]", w):
            out.append(re.split(r"[ =]+", w[2:], 1)[1].replace(" ", ":"))
    return out


def _open_bind(addr: str) -> bool:
    """Whether a bind address listens beyond loopback: empty, `*`, or anything but a loopback name or address."""
    return not _loopback(addr)


def _socat_exposes(argv: list[str]) -> bool:
    for w in argv[1:]:
        if re.match(r"(?i)^[a-z0-9]+-l(?:isten)?:", w):
            opts = w.split(",")[1:]
            bind = next((o.split("=", 1)[1] for o in opts if o.lower().startswith("bind=")), "")
            if _open_bind(bind):
                return True
    return False


def _nc_exposes(argv: list[str]) -> bool:
    if not any(w == "--listen" or re.fullmatch(r"-[a-zA-Z0-9]*l[a-zA-Z0-9]*", w) for w in argv[1:]):
        return False
    addrs = [w for w in argv[1:] if re.fullmatch(r"(?:\d{1,3}(?:\.\d{1,3}){3}|\*|localhost|::1?|\[[0-9a-f:]+\])", w, re.I)]
    return not addrs or any(_open_bind(a) for a in addrs)


def proxy_refusal(cmd: str, segments: list[list[str]], port_set: set[str]) -> str | None:
    """The refusal for a listener or firewall rule exposing one of `port_set`, else None."""
    for port in sorted(port_set):
        if not _has_port(cmd, port):
            continue
        why = PROXY_WHY.format(port=port)
        for argv in segments:
            tool = next((b for b in map(_base, argv[:5]) if b in GUARDED_TOOLS or INTERPRETERS_RE.match(b)), _base(argv[0]))
            text = " ".join(argv)
            if tool in ("socat", "socat1") and _socat_exposes(argv):
                return why
            if tool in NC_TOOLS and _nc_exposes(argv):
                return why
            if tool in ("ssh", "autossh"):
                widened = bool(SSH_GATEWAY_RE.search(text)) or any(SSH_G_FLAG_RE.match(w) for w in argv[1:])
                for spec in _ssh_forwards(argv):
                    fwd = _forward_bind(spec)
                    if fwd and fwd[1] == port and (widened or fwd[0] is not None and _open_bind(fwd[0])):
                        return why
            if tool in FIREWALL_TOOLS and _has_port(text, port):
                if re.search(r"(?:^|\s)(?:-D|--delete)\b|-j\s+(?:DROP|REJECT)\b|\b(?:deny|reject|drop|delete)\b", text):
                    continue
                return why
            if INTERPRETERS_RE.match(tool) and re.search(r"0\.0\.0\.0|INADDR_ANY|(?<![\d.])(?!127\.)\d{1,3}(?:\.\d{1,3}){3}(?![\d.])", text) \
                    and re.search(r"\b(?:bind|listen|createServer|TCPServer|serve_forever|socketserver)\b", text, re.I):
                return why
    return None


def cred_refusal(cmd: str, segments: list[list[str]], env: dict) -> str | None:
    """The refusal for handing the run's credential variables or token value to a container, another agent or
    an env file, else None."""
    names = list(CRED_VARS)   # all of them, set here or not: a container gets whatever its parent holds
    token = str(env.get("ANTHROPIC_AUTH_TOKEN") or "")
    token = token if len(token) >= 8 else ""
    named = "|".join(names)
    expand = re.compile(rf"\$\{{?(?:{named})(?![\w])") if names else None
    bare = re.compile(rf"^(?:-[\w-]*=|-e)?(?:{named})$") if names else None
    hit = [argv for argv in segments if argv and any(_base(w) in CONTAINER_TOOLS for w in argv)]
    if hit:
        if expand and expand.search(cmd) or token and token in cmd:
            return CRED_WHY.format(names=", ".join(names))
        if bare and not re.search(r"\bttp\s+secret\s+env\b", cmd) and any(bare.match(w) for argv in hit for w in argv[1:]):
            return CRED_WHY.format(names=", ".join(names))
    if hit and ENV_DUMP_RE.search(cmd) and any(re.match(r"--env-file(?:=|$)", w) for argv in hit for w in argv[1:]):
        return CRED_WHY.format(names=", ".join(names))
    if ENV_FILE_RE.search(cmd):
        if expand and expand.search(cmd) or token and token in cmd \
                or named and re.search(rf"(?<![\w$]){'(?:' + named + ')'}(?![\w=])", cmd):
            return CRED_WHY.format(names=", ".join(names))
    return None


def refusal(cmd: str, segments: list[list[str]], env: dict | None = None) -> str | None:
    """Why the worker Bash command `cmd` (its simple commands `segments`, shell wrappers looked through) is
    refused, else None. Commands that name none of the guarded things are never looked at closely; a command
    this guard cannot judge passes."""
    env = dict(os.environ) if env is None else env
    try:
        return cred_refusal(cmd, segments, env) or proxy_refusal(cmd, segments, proxy_ports(env))
    except Exception:
        return None
