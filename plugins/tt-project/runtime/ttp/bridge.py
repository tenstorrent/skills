# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""An ssh access bridge (`ttp bridge`): lab boxes reach hosts through this computer's ssh agents,
with no workstation key or credential ever stored on a box (offline certificate mode, when on, adds
the box's own key, made on the box by `--make-key`, and a short-lived certificate for it).

This computer (the user's workstation) keeps one ssh session per box and agent as a user service
(launchd agent on macOS, systemd user unit on Linux; restarted on every drop, after sleep and at
login). Each session forwards exactly one of this computer's agents and links the box's end of it
to a stable path, `<socket_dir>/<agent>.sock` (default `~/.ssh/tt-agent`), and stays up only while
that agent socket exists. Hosts the box cannot reach by itself are reverse-forwarded from this
computer to a loopback port on the box (`-R 127.0.0.1:<port>:<host>:22`, ssh ports only), so the
hosts' own source-address limits still see this computer. Hosts the box reaches by itself (GitHub,
say) only get the agent; their traffic never passes through this computer.

On the box the tool writes one marked block at the top of ~/.ssh/config (the old file backed up
first, an older block replaced, everything else kept): a Host entry per host naming its loopback
port and `IdentityAgent <socket_dir>/<agent>.sock`. The block has no IdentityFile and nothing
else holds key material: the private keys stay in this computer's agents.

It fails closed. When this computer sleeps, loses the network or stops the session, the reverse
forwards close and the agent link dangles, so a login through it fails; nothing is cached on the
box. Every process of the box account can use a forwarded agent while its session is up: forward
only agents whose keys that account may use, and prefer agents with confirmation or destination
limits where the agent offers them. The link folder must be a private folder of the box account
(its own, 0700, not a symlink) or the session exits. Reverse forwards bind the box's loopback only:
other accounts on the box can open those ports, which reach the hosts' sshd and still need a key
(a box sshd with `GatewayPorts yes` would bind them on every interface: keep it off).
On this computer the interface relay listens only on unix sockets (folder 0700, sockets 0600).

Stale sessions recover by themselves. Every session runs inside the relay wrapper
(bridge_relay.py), which tags its remote command with a random id of this computer, the box, the
agent and the session. When a network change leaves the box's sshd holding an earlier session's
reverse-forward ports, the new session fails with 'remote port forwarding failed'; the wrapper then
makes one cleanup login (no forwards, no agent, a short timeout) that ends only the box account's
own sshd sessions with this computer's tag for the same box and agent listening on 127.0.0.1 at one
of this session's ports, logs one line per ended session, and starts the session again at once. A
second forward failure in a row exits, so the service's restart pacing applies. No sudo, no other
account, no untagged or interactive session, no box sshd change. `ttp setup` and upgrades re-install
services whose command changed, so an older workstation picks this up with no step of its own.

A dead agent behind a live-looking link recovers too. A session whose workstation agent died keeps
its forwarded socket file, so a link that only checks the file looks fine while every login through
it fails with 'Permission denied (publickey)'. The box-side loop asks the agents for their keys every
30 s: a link whose agent does not answer while this session's does is relinked to this session's;
this session's own agent failing twice in a row ends the session, and the wrapper reconnects
(within about a minute of the agent dying, then bounded retries). The box's daemon checks each link
the block names once a minute on its own (bridgewatch.py): a low status line at once, one normal
alert per outage past bridge.agent_alert_after_min, naming the bridge's agent and hosts; a drop
before the agent answered steadily for bridge.agent_steady_min continues the same outage.

The config is JSON (`~/.config/tt-project/bridge.json`, or --config); see
docs/access-bridge.example.json.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import sshcmd
from .project import HOME_DIR, durable_write

CONFIG = Path.home() / ".config" / "tt-project" / "bridge.json"
SOCKET_DIR = "~/.ssh/tt-agent"
BEGIN = "# >>> tt-project access bridge (ttp bridge; managed, edits inside are replaced) >>>"
END = "# <<< tt-project access bridge <<<"
ENV_AGENT = "TTP_BRIDGE_AGENT"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
# Words that never belong in the box's block: a key file, key text, or a route that would carry
# traffic other than one ssh login (a SOCKS proxy, a jump through this computer).
FORBIDDEN = re.compile(r"(?im)^\s*(IdentityFile|CertificateFile|ForwardAgent|DynamicForward|"
                       r"ProxyCommand|ProxyJump|LocalForward|RemoteForward|PKCS11Provider)\b|PRIVATE KEY")


# a forwarded host is a loopback port here: an older entry for one of its names further down (a proxy,
# or IdentitiesOnly with a key file) must not take over, and the first value ssh reads wins
PROXY_NONE = "  ProxyCommand none"


class BadConfig(ValueError):
    pass


def _name(v, what: str) -> str:
    if not isinstance(v, str) or not NAME.match(v):
        raise BadConfig(f"{what} {v!r}: use letters, digits, '.', '_' or '-' (no spaces, not starting with '-')")
    return v


def _port(v, what: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not 1024 <= v <= 65535:
        raise BadConfig(f"{what}: a port from 1024 to 65535, not {v!r}")
    return v


def parse(data: dict) -> dict:
    """The checked config: {"socket_dir", "boxes": {box: {"login": {...}, "agents": {agent: {...}}}}}.
    Each agent: {"agent": "env" or a socket path on this computer, "forward": [host...], "direct": [host...]}.
    A forwarded host: {"name", "aliases", "target", "port", "adopt", "user", "host_key_alias", "interface"}
    ("interface": this computer reaches the target only bound to that interface; see bridge_relay); a direct
    one: {"name", "aliases", "user", "target", "host_key_alias"}. A box's optional "hold_check": a command on
    the box, run as `<it> %n %h`, exiting 0 for a host someone holds (its refusal is a proxy set further down,
    which the block then leaves in place) and 1 for a free one; any other exit (a missing or broken check)
    counts as held, so the check can only keep a hold, never lift one). (the last two optional: the HostName to dial
    and the known-hosts name to check it by, for a host whose older config entries elsewhere name others).
    Raises BadConfig naming the first problem."""
    if not isinstance(data, dict):
        raise BadConfig("the config is a JSON object")
    sock = data.get("socket_dir", SOCKET_DIR)
    if not isinstance(sock, str) or not sock.startswith("~/") or re.search(r"[^A-Za-z0-9_./~-]", sock) or ".." in sock:
        raise BadConfig(f"socket_dir {sock!r}: a plain path under the box account's home, starting with '~/'")
    boxes = data.get("boxes")
    if not isinstance(boxes, dict) or not boxes:
        raise BadConfig("boxes: one entry per box (its ssh alias on this computer)")
    out: dict = {"socket_dir": sock.rstrip("/"), "boxes": {}}
    for box, b in boxes.items():
        _name(box, "box")
        if not isinstance(b, dict):
            raise BadConfig(f"box {box}: an object")
        login = b.get("login") or {}
        if not isinstance(login, dict) or set(login) - {"agent", "identity"}:
            raise BadConfig(f"box {box}: login takes only 'agent' (a socket) and 'identity' (a .pub file)")
        if login.get("identity") and not str(login["identity"]).endswith(".pub"):
            raise BadConfig(f"box {box}: login.identity names the public key (.pub); the private key stays in the agent")
        agents = b.get("agents")
        if not isinstance(agents, dict) or not agents:
            raise BadConfig(f"box {box}: agents: at least one")
        ports: dict = {}
        names: dict = {}
        parsed: dict = {}
        for an, a in agents.items():
            _name(an, f"box {box}: agent")
            if not isinstance(a, dict) or not isinstance(a.get("agent"), str) or not a["agent"]:
                raise BadConfig(f"box {box} agent {an}: 'agent' is 'env' (this computer's SSH_AUTH_SOCK) or a socket path")
            fwd, direct = [], []
            for h in a.get("forward") or []:
                if not isinstance(h, dict):
                    raise BadConfig(f"box {box} agent {an}: each forward entry is an object")
                hn = _name(h.get("name"), f"box {box} agent {an}: host")
                target = h.get("target") or hn
                if not isinstance(target, str) or target.startswith("-") or re.search(r"[\s:/@]", target):
                    raise BadConfig(f"box {box} host {hn}: target {target!r} is a host name or address (its ssh port 22 is forwarded)")
                port = _port(h.get("port"), f"box {box} host {hn}: port")
                if port in ports and not (h.get("adopt") and ports[port] == target):
                    raise BadConfig(f"box {box}: port {port} is used twice")
                ports[port] = target
                fwd.append({"name": hn, "aliases": [_name(x, f"host {hn}: alias") for x in h.get("aliases") or []],
                            "target": target, "port": port, "adopt": bool(h.get("adopt")),
                            "user": _name(h["user"], f"host {hn}: user") if h.get("user") else "",
                            "host_key_alias": _name(h.get("host_key_alias") or target, f"host {hn}: host_key_alias"),
                            "interface": _name(h["interface"], f"host {hn}: interface") if h.get("interface") else ""})
            for h in a.get("direct") or []:
                if not isinstance(h, dict):
                    raise BadConfig(f"box {box} agent {an}: each direct entry is an object")
                direct.append({"name": _name(h.get("name"), f"box {box} agent {an}: host"),
                               "aliases": [_name(x, "alias") for x in h.get("aliases") or []],
                               "user": _name(h["user"], "user") if h.get("user") else "",
                               "target": _name(h["target"], "target") if h.get("target") else "",
                               "host_key_alias": _name(h["host_key_alias"], "host_key_alias") if h.get("host_key_alias") else ""})
            if not fwd and not direct:
                raise BadConfig(f"box {box} agent {an}: list the hosts it serves (forward or direct)")
            for h in fwd + direct:
                for n in [h["name"], *h["aliases"]]:
                    if n in names:
                        raise BadConfig(f"box {box}: host name {n} appears twice")
                    names[n] = an
            parsed[an] = {"agent": a["agent"], "forward": fwd, "direct": direct}
        hold = b.get("hold_check") or ""
        if hold and (not isinstance(hold, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", hold)):
            raise BadConfig(f"box {box}: hold_check is an absolute path on the box (letters, digits, '_', '.', '-', '/')")
        if not isinstance(b.get("gh_pull", False), bool):
            raise BadConfig(f"box {box}: gh_pull is true or false (this computer runs the box's queued gh calls)")
        out["boxes"][box] = {"login": {"agent": login.get("agent", ""), "identity": login.get("identity", "")},
                             "agents": parsed, "hold_check": hold, "gh_pull": b.get("gh_pull", False)}
    if data.get("cert") is not None:
        from . import bridge_cert
        out["cert"] = bridge_cert.parse(data["cert"], out)
    return out


def load(path: Path | None = None) -> dict:
    path = Path(path or CONFIG).expanduser()
    try:
        data = json.loads(path.read_text())
    except OSError as e:
        raise BadConfig(f"no bridge config at {path} ({e.strerror}); start from docs/access-bridge.example.json")
    except ValueError as e:
        raise BadConfig(f"{path} is not JSON: {e}")
    return parse(data)


def _remote(path: str) -> str:
    """A '~/...' path as the box's shell reads it, quoted."""
    return '"$HOME/' + path[2:] + '"'


def link(cfg: dict, agent: str) -> str:
    return f"{cfg['socket_dir']}/{agent}.sock"


def _local_path(p: str) -> str:
    return str(Path(p).expanduser())


def _opt(key: str, value: str) -> str:
    """-o value; ssh splits on spaces, so a path with one is quoted for it."""
    return f'{key}="{value}"' if re.search(r"\s", value) else f"{key}={value}"


CHECK_S = 30     # how often the box-side loop asks the agent behind the link for its keys
AGENT_DEAD = 5   # its exit after two checks in a row found its own forwarded agent dead: the wrapper reconnects
# `ok <socket>`: the agent there answers (ssh-add -l exits 0, or 1 with no keys); 2, a timeout or a
# failed exchange is a dead agent. A session whose workstation agent died keeps its socket file, so
# `-S` alone never notices.
AGENT_OK = ("ok() { t=; command -v timeout >/dev/null 2>&1 && t='timeout 10'; "
            'o=$(SSH_AUTH_SOCK="$1" $t ssh-add -l 2>&1); r=$?; [ $r -eq 0 ] || { [ $r -eq 1 ] && '
            "! printf '%s' \"$o\" | grep -qiE 'error fetching|communication with agent'; }; }")


def remote_command(cfg: dict, agent: str) -> str:
    """Run on the box: link the forwarded agent to its stable path and stay up while it answers.
    Every CHECK_S it asks the agents for their keys: a link whose agent does not answer while this
    session's does is relinked here; this session's own agent failing twice in a row exits AGENT_DEAD,
    so the wrapper reconnects (a dead agent is noticed within about a minute).
    Exit 3 when the box's sshd gave no agent (the login key's authorized_keys line forbids it), 4 when
    the link folder is not a private folder of the box account (its own, 0700, not a symlink).
    The folder and link are touched only when they change: a reconnect to the same path leaves them."""
    d = _remote(cfg["socket_dir"])
    sock = _remote(link(cfg, agent))
    private = f'[ -n "$(find {d} -prune -type d -user "$(id -u)" -perm 700)" ]'
    refuse = f'{{ echo "ttp bridge: {cfg["socket_dir"]} is not a private folder of this account" >&2; exit 4; }}'
    return (f'[ -n "$SSH_AUTH_SOCK" ] || {{ echo "ttp bridge: no forwarded agent (the login key may not forward one)" >&2; exit 3; }}; '
            f"umask 077; [ ! -L {d} ] || {refuse}; [ -d {d} ] || mkdir -p {d}; "
            f"{private} || chmod 700 {d} 2>/dev/null; {private} || {refuse}; "
            f'[ "$(readlink {sock} 2>/dev/null)" = "$SSH_AUTH_SOCK" ] || ln -sfn "$SSH_AUTH_SOCK" {sock} || exit 4; '
            f'echo "ttp bridge: {agent} agent linked"; {AGENT_OK}; n=0; while [ -S "$SSH_AUTH_SOCK" ]; do sleep {CHECK_S}; '
            f'if ok "$SSH_AUTH_SOCK"; then n=0; [ "$(readlink {sock} 2>/dev/null)" = "$SSH_AUTH_SOCK" ] || ok {sock} || '
            f'{{ ln -sfn "$SSH_AUTH_SOCK" {sock} && echo "ttp bridge: {agent} agent relinked (the link\'s agent did not answer)"; }}; '
            f'else n=$((n+1)); [ $n -lt 2 ] || {{ echo "ttp bridge: {agent} forwarded agent stopped answering; reconnecting" >&2; '
            f'exit {AGENT_DEAD}; }}; fi; done')


def ssh_argv(cfg: dict, box: str, agent: str, ssh: str | None = None) -> list[str]:
    """The session for one box and agent: forwards that agent and the reverse forwards of its
    hosts; its own connection (never a shared ControlMaster); exits on a lost forward or link."""
    sshcmd.check_host(box)
    b = cfg["boxes"][box]
    a = b["agents"][agent]
    # The system ssh, never a wrapper earlier on this run's PATH: the service outlives the run.
    system = "/usr/bin/ssh" if os.path.exists("/usr/bin/ssh") else shutil.which("ssh") or "ssh"
    argv = [ssh or system, "-T", "-n", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=20",
            "-o", "ControlMaster=no", "-o", "ControlPath=none"]
    if b["login"]["agent"]:
        argv += ["-o", _opt("IdentityAgent", _local_path(b["login"]["agent"]))]
    if b["login"]["identity"]:
        argv += ["-o", "IdentitiesOnly=yes", "-o", _opt("IdentityFile", _local_path(b["login"]["identity"]))]
    if a["agent"] == "env":
        # ssh points SSH_AUTH_SOCK at its IdentityAgent, so ForwardAgent=yes would forward the login's
        # agent; the service's own agent goes under a name of its own instead.
        argv = ["/bin/sh", "-c", f'{ENV_AGENT}="$SSH_AUTH_SOCK"; export {ENV_AGENT}; exec "$@"', "ttp-bridge",
                *argv, "-o", f"ForwardAgent=${{{ENV_AGENT}}}"]
    else:
        argv += ["-o", _opt("ForwardAgent", _local_path(a["agent"]))]
    for h in a["forward"]:
        if h["adopt"]:
            continue
        dest = str(relay_dir(box, agent) / f"{h['name']}.sock") if h["interface"] else f"{h['target']}:22"
        argv += ["-R", f"127.0.0.1:{h['port']}:{dest}"]
    return argv + ["--", box, remote_command(cfg, agent)]


def relay_dir(box: str, agent: str) -> Path:
    return HOME_DIR / "bridge-relay" / f"{box}.{agent}"


RELAY = HOME_DIR / "bridge_relay.py"


def service_argv(cfg: dict, box: str, agent: str, ssh: str | None = None) -> list[str]:
    """What the service runs: the session inside the relay wrapper (a copy of bridge_relay.py, installed
    by setup), which tags it, recovers from a stale session holding its ports, and relays the hosts
    reached through one interface."""
    argv = ssh_argv(cfg, box, agent, ssh)
    bound = [h for h in cfg["boxes"][box]["agents"][agent]["forward"] if h["interface"] and not h["adopt"]]
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    return [py, str(RELAY), "--tag", f"{box}/{agent}", str(relay_dir(box, agent)),
            *[f"{h['name']}={h['target']}%{h['interface']}" for h in bound], "--", *argv]


def install_relay() -> None:
    """Copy bridge_relay.py where the service runs it, only when its content changed."""
    data = Path(__file__).with_name("bridge_relay.py").read_bytes()
    try:
        if RELAY.read_bytes() == data:
            return
    except OSError:
        pass
    durable_write(RELAY, data, mode=0o600)


def render_block(cfg: dict, box: str) -> str:
    """The box's marked ~/.ssh/config block. No IdentityFile, no proxy: an agent socket and loopback ports only.
    It ends with `Match all` so what follows in the file is not scoped to its last Host."""
    from . import bridge_cert
    offline, allowed = bridge_cert.block_lines(cfg, box)
    hold = cfg["boxes"][box].get("hold_check", "")
    lines = [BEGIN, "# Logins here use this box's link to an agent on the user's workstation; they fail while",
             "# that workstation is asleep or its session is down" + (
                 " and the certificate below has ended." if offline else ". No key is stored on this box.")]
    lines += offline
    for an, a in cfg["boxes"][box]["agents"].items():
        ident = link(cfg, an)
        for h in a["forward"]:
            lines += ["", f"Host {' '.join([h['name'], *h['aliases']])}", "  HostName 127.0.0.1", f"  Port {h['port']}",
                      f"  HostKeyAlias {h['host_key_alias']}", "  StrictHostKeyChecking accept-new",
                      f"  IdentityAgent {ident}", *([] if hold else [PROXY_NONE]), "  IdentitiesOnly no",
                      "  ServerAliveInterval 15", "  ServerAliveCountMax 3"]
            if h["user"]:
                lines.append(f"  User {h['user']}")
        for h in a["direct"]:
            lines += ["", f"Host {' '.join([h['name'], *h['aliases']])}", f"  IdentityAgent {ident}"]
            if h["target"]:
                lines.append(f"  HostName {h['target']}")
            if h["host_key_alias"]:
                lines.append(f"  HostKeyAlias {h['host_key_alias']}")
            if h["user"]:
                lines.append(f"  User {h['user']}")
    if hold:   # the proxy goes off only for a host its hold check does not hold, so a hold keeps refusing
        names = [n for a in cfg["boxes"][box]["agents"].values() for h in a["forward"] for n in [h["name"], *h["aliases"]]]
        if names:
            # only a clear "not held" (exit 1) lifts the proxy: a missing or failing check keeps the hold
            lines += ["", f"Match originalhost {','.join(names)} exec \"sh -c '{hold} %n %h; [ $? -eq 1 ]'\"", PROXY_NONE]
    block = "\n".join(lines + ["", "Match all", END]) + "\n"
    check_block(block, allowed)
    return block


def check_block(block: str, allowed: set[str] | frozenset = frozenset()) -> None:
    """Fail closed: refuse a block that would name a key file, hold key text or proxy traffic. The only
    key-file lines let through are the exact ones of the offline certificate entries (allowed), and the
    only proxy line is `ProxyCommand none`, which turns off a proxy an older entry further down sets."""
    for m in FORBIDDEN.finditer(block):
        start, end = block.rfind("\n", 0, m.start(1)) + 1, block.find("\n", m.end(1))
        line = block[start:end if end >= 0 else None]
        if m.group(1) in ("IdentityFile", "CertificateFile") and line in allowed:
            continue
        if line == PROXY_NONE:
            continue
        raise BadConfig(f"refusing the box block: it has {m.group(0).strip()!r} (no key files, keys or proxies on a box)")


def merge(old: str, block: str) -> str:
    """The box's ssh config with the block at the top: an earlier block (wherever it is) replaced,
    every other line kept as it was. Its Host entries come first so they win over older ones."""
    rest = strip(old)
    return block + ("\n" + rest if rest.strip() else "")


def strip(text: str) -> str:
    """text without the marked block."""
    out, inside = [], False
    for ln in text.splitlines(keepends=True):
        if ln.rstrip("\n") == BEGIN:
            inside = True
        elif inside and ln.rstrip("\n") == END:
            inside = False
        elif not inside:
            out.append(ln)
    return "".join(out).lstrip("\n")


# --- this computer's user services -------------------------------------------------------------

GH_PULL = "gh-pull"   # the service running a box's queued gh calls here (`ttp bridge gh-pull --loop`)


def label(box: str, agent: str) -> str:
    return f"com.tt-project.bridge.{box}.{agent}"


def service_file(box: str, agent: str, platform: str | None = None) -> Path:
    if (platform or sys.platform) == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{label(box, agent)}.plist"
    from . import hoststate
    return hoststate.unit_dir() / f"{label(box, agent)}.service"


def launchd_plist(box: str, agent: str, argv: list[str]) -> dict:
    log = str(HOME_DIR / f"bridge-{box}-{agent}.log")
    return {"Label": label(box, agent), "ProgramArguments": argv, "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 30, "ProcessType": "Background", "StandardOutPath": log, "StandardErrorPath": log}


def systemd_unit(box: str, agent: str, argv: list[str]) -> str:
    from .tunnel import _unit_escape
    return (f"[Unit]\nDescription=tt-project: access bridge to {box} ({agent} agent)\nStartLimitIntervalSec=0\n\n"
            f"[Service]\nExecStart={_unit_escape(' '.join(shlex.quote(a) for a in argv))}\n"
            f"Restart=always\nRestartSec=5\nRestartSteps=10\nRestartMaxDelaySec=300\n\n"
            f"[Install]\nWantedBy=default.target\n")


def _run(argv: list[str], timeout: int = 60, inp: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=inp)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout}s")


def _box(box: str, script: str, timeout: int = 40, inp: str | None = None) -> subprocess.CompletedProcess:
    """One plain login to the box (its own connection) running script."""
    return _run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=no",
                 "-o", "ControlPath=none", "--", sshcmd.check_host(box), script], timeout, inp)


def start(box: str, agent: str, argv: list[str], platform: str | None = None) -> str:
    platform = platform or sys.platform
    f = service_file(box, agent, platform)
    f.parent.mkdir(parents=True, exist_ok=True)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        durable_write(f, plistlib.dumps(launchd_plist(box, agent, argv)))
        from .service import _reload   # bootout, then bootstrap retried past launchd's error 5 right after it
        r = _reload(label(box, agent), f, lambda *a: _run(list(a)))
        return ("launchd agent started" if r.returncode == 0
                else f"launchd refused it: {(r.stderr or r.stdout).strip()[:200]}")
    durable_write(f, systemd_unit(box, agent, argv))
    _run(["systemctl", "--user", "daemon-reload"])
    r = _run(["systemctl", "--user", "enable", "--now", "--", f"{label(box, agent)}.service"])
    if r.returncode == 0:
        _run(["systemctl", "--user", "restart", "--", f"{label(box, agent)}.service"])
    return "systemd user unit started" if r.returncode == 0 else f"systemd refused it: {r.stderr.strip()[:200]}"


def stop(box: str, agent: str, platform: str | None = None, remove: bool = False) -> str:
    """Stop the session (its forwards close and the box's link dangles); remove: also its service file."""
    platform = platform or sys.platform
    f = service_file(box, agent, platform)
    if not f.is_file():
        return "not installed"
    if platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{label(box, agent)}"])
    else:
        _run(["systemctl", "--user", "disable" if remove else "stop", "--now", "--", f"{label(box, agent)}.service"])
    if remove:
        f.unlink()
    return "stopped and removed" if remove else "stopped"


def running(box: str, agent: str, platform: str | None = None) -> bool:
    platform = platform or sys.platform
    if platform == "darwin":
        r = _run(["launchctl", "print", f"gui/{os.getuid()}/{label(box, agent)}"])
        return r.returncode == 0 and bool(re.search(r"^\s*state = running", r.stdout, re.M))
    return _run(["systemctl", "--user", "is-active", "--quiet", "--", f"{label(box, agent)}.service"]).returncode == 0


def write_block(cfg: dict, box: str) -> str:
    """Write the block into the box's ~/.ssh/config (backup first, merged). Returns what was done."""
    r = _box(box, 'cat "$HOME/.ssh/config" 2>/dev/null; true')
    if r.returncode != 0:
        raise RuntimeError(f"could not read {box}'s ssh config: {r.stderr.strip()[:200]}")
    old = r.stdout
    new = merge(old, render_block(cfg, box))
    if new == old:
        return "block already current"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    w = _box(box, replace_config_script(stamp), inp=new)
    if w.returncode != 0:
        raise RuntimeError(f"could not write {box}'s ssh config: {w.stderr.strip()[:200]}")
    if "unchanged" in w.stdout:
        return "block already current"
    return f"block written (backup ~/.ssh/config.bak-ttp-bridge-{stamp})" if old else "block written (new config)"


def remove_block(box: str) -> str:
    r = _box(box, 'cat "$HOME/.ssh/config" 2>/dev/null; true')
    if BEGIN not in r.stdout:
        return "no block"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    w = _box(box, replace_config_script(stamp), inp=strip(r.stdout))
    if w.returncode != 0:
        return f"could not remove the block: {w.stderr.strip()[:200]}"
    return "no block" if "unchanged" in w.stdout else "block removed"


def replace_config_script(stamp: str) -> str:
    """Run on the box with the new ~/.ssh/config on stdin: staged outside ~/.ssh and compared there, so
    a run that changes nothing writes nothing in ~/.ssh; else a backup, then an atomic rename."""
    return ('set -e; umask 077; c="$HOME/.ssh/config"; t=$(mktemp "${TMPDIR:-/tmp}/ttp-bridge.XXXXXX"); '
            'trap \'rm -f "$t"\' EXIT; cat > "$t"; '
            'if [ -e "$c" ] && cmp -s "$t" "$c"; then echo unchanged; exit 0; fi; '
            'mkdir -p "$HOME/.ssh"; '
            f'if [ -e "$c" ]; then cp -p "$c" "$c.bak-ttp-bridge-{stamp}"; fi; '
            'cp "$t" "$c.ttp-bridge.tmp"; chmod 600 "$c.ttp-bridge.tmp"; mv "$c.ttp-bridge.tmp" "$c"; echo written')


def setup(cfg: dict, boxes: list[str], platform: str | None = None) -> list[str]:
    out = []
    for box in boxes:
        out.append(f"{box}: {write_block(cfg, box)}")
        for an in cfg["boxes"][box]["agents"]:
            argv = service_argv(cfg, box, an)
            if argv[1:2] == [str(RELAY)]:
                install_relay()
            out.append(f"{box} {an}: {start(box, an, argv, platform)}")
        if cfg["boxes"][box].get("gh_pull"):   # its delivery.gh_route "workstation" calls (ghqueue)
            from .ghqueue import service_argv as gh_argv
            out.append(f"{box} {GH_PULL}: {start(box, GH_PULL, gh_argv(box), platform)}")
    return out


def installed_argv(box: str, agent: str, platform: str | None = None) -> list[str] | None:
    """The command an installed service runs, or None when it is not installed (or unreadable)."""
    f = service_file(box, agent, platform)
    try:
        if (platform or sys.platform) == "darwin":
            return list(plistlib.loads(f.read_bytes()).get("ProgramArguments") or [])
        for ln in f.read_text().splitlines():
            if ln.startswith("ExecStart="):
                from .tunnel import _unit_unescape
                return shlex.split(_unit_unescape(ln[len("ExecStart="):]))
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return None


def refresh(platform: str | None = None, path: Path | None = None, restarted: dict | None = None) -> list[str]:
    """After `ttp setup` or an upgrade: re-install the relay copy and restart each installed session
    whose command changed (into `restarted`: (box, agent) -> what the restart said). Nothing when no
    bridge is configured or installed here."""
    if os.environ.get("TTP_TEST_NO_BRIDGE_REFRESH") or not (path or CONFIG).is_file():
        return []
    try:
        cfg = load(path)
    except (BadConfig, OSError, ValueError) as e:
        return [f"bridge: config not read ({e}); services left as they are"]
    out = []
    for box, b in cfg["boxes"].items():
        for an in b["agents"]:
            have = installed_argv(box, an, platform)
            if have is None:
                continue
            argv = service_argv(cfg, box, an)
            install_relay()
            if have != argv:
                said = start(box, an, argv, platform)
                if restarted is not None:
                    restarted[(box, an)] = said
                out.append(f"bridge {box} {an}: service command changed; {said}")
    return out


CHECK_WAIT_S = 30   # a restarted session needs a few seconds to log in and forward its links


def refresh_checked(platform: str | None = None, path: Path | None = None,
                    wait_s: float | None = None) -> tuple[list[str], int]:
    """refresh, then `ttp bridge status`'s check of each box whose session it restarted, retried while the
    sessions connect (at most wait_s). rc 1, with the links still down and launchd's answer, when any is."""
    restarted: dict = {}
    out = refresh(platform, path, restarted)
    if not restarted:
        return out, 0
    cfg = load(path)
    boxes = sorted({box for box, _ in restarted})
    deadline = time.time() + (CHECK_WAIT_S if wait_s is None else wait_s)
    while True:
        lines, rc = status(cfg, boxes, platform)
        if rc == 0 or time.time() >= deadline:
            break
        time.sleep(3)
    out += [f"bridge status: {ln}" for ln in lines]
    if rc:
        down = [f"{box} {an}" + (f" ({said})" if "refused" in said else "")
                for (box, an), said in sorted(restarted.items())
                if any(ln.startswith((f"{box} {an}: DOWN", f"{box}: ")) for ln in lines)]
        out.append(f"bridge: still down after its restart: {'; '.join(down) or ', '.join(boxes)}; "
                   f"`ttp bridge status` shows it, `ttp bridge setup <box>` restarts it")
    return out, rc


def probe_script(cfg: dict, box: str) -> str:
    """Read-only, on the box: per agent whether its link answers and how many keys it offers; per
    forwarded host whether its port listens; the block's presence; what the socket dir holds."""
    b = cfg["boxes"][box]
    parts = [f'd={_remote(cfg["socket_dir"])}',
             f'grep -qxF {shlex.quote(BEGIN)} "$HOME/.ssh/config" 2>/dev/null && echo "block yes" || echo "block no"']
    for an, a in b["agents"].items():
        s = _remote(link(cfg, an))
        parts.append(f'if [ -S {s} ]; then n=$(SSH_AUTH_SOCK={s} ssh-add -l 2>/dev/null | grep -c SHA256); '
                     f'echo "agent {an} up $n"; else echo "agent {an} down 0"; fi')
        for h in a["forward"]:
            parts.append(f'(exec 3<>/dev/tcp/127.0.0.1/{h["port"]}) 2>/dev/null && echo "port {h["port"]} up" '
                         f'|| echo "port {h["port"]} down"')
    parts.append('for f in "$d"/* "$d"/.*; do [ -e "$f" ] || [ -L "$f" ] || continue; case "$f" in */.|*/..) continue;; esac; '
                 'if [ -S "$f" ] || [ -L "$f" ]; then echo "file sock ${f##*/}"; else echo "file other ${f##*/}"; fi; done')
    return "bash -c " + shlex.quote("; ".join(parts))


def parse_probe(text: str) -> dict:
    out: dict = {"block": False, "agents": {}, "ports": {}, "other_files": []}
    for ln in text.splitlines():
        w = ln.split()
        if w[:1] == ["block"]:
            out["block"] = w[1:2] == ["yes"]
        elif w[:1] == ["agent"] and len(w) == 4:
            out["agents"][w[1]] = {"up": w[2] == "up", "keys": int(w[3]) if w[3].isdigit() else 0}
        elif w[:1] == ["port"] and len(w) == 3 and w[1].isdigit():
            out["ports"][int(w[1])] = w[2] == "up"
        elif w[:2] == ["file", "other"]:
            out["other_files"].append(" ".join(w[2:]))
    return out


def status(cfg: dict, boxes: list[str], platform: str | None = None, deep: bool = False) -> tuple[list[str], int]:
    """One line per box and agent; deep (doctor): also what each problem means. rc 1 when any is down."""
    lines, bad = [], 0
    for box in boxes:
        b = cfg["boxes"][box]
        r = _box(box, probe_script(cfg, box))
        if r.returncode == 255:
            lines.append(f"{box}: unreachable from here ({r.stderr.strip()[-160:]})")
            bad += 1
            continue
        p = parse_probe(r.stdout)
        if not p["block"]:
            bad += 1
            lines.append(f"{box}: the ssh config block is missing (`ttp bridge setup {box}`)")
        if p["other_files"]:
            bad += 1
            lines.append(f"{box}: the socket dir holds files that are not sockets or links: {', '.join(p['other_files'])} "
                         "(nothing but agent links belongs there)")
        for an, a in b["agents"].items():
            svc = "running" if running(box, an, platform) else ("stopped" if service_file(box, an, platform).is_file()
                                                                else "not installed")
            ag = p["agents"].get(an, {"up": False, "keys": 0})
            ports = [f"{h['name']}:{h['port']} {'up' if p['ports'].get(h['port']) else 'down'}" for h in a["forward"]]
            ok = svc == "running" and ag["up"] and ag["keys"] > 0 and all(p["ports"].get(h["port"]) for h in a["forward"])
            bad += not ok
            line = (f"{box} {an}: {'OK' if ok else 'DOWN'} (session {svc}; agent link "
                    f"{'up, ' + str(ag['keys']) + ' key(s)' if ag['up'] else 'down'}"
                    + (f"; {', '.join(ports)}" if ports else "")
                    + (f"; direct: {', '.join(h['name'] for h in a['direct'])}" if a["direct"] else "") + ")")
            lines.append(line)
            if deep and not ok:
                log = HOME_DIR / f"bridge-{box}-{an}.log"
                tail = log.read_text(errors="replace").strip().splitlines()[-3:] if log.is_file() else []
                held = [f"{h['name']}:{h['port']}" for h in a["forward"] if p["ports"].get(h["port"]) and not ag["up"]]
                if held:
                    lines.append(f"  port {', '.join(held)} listens on the box while this session's link is down: an "
                                 "earlier (stale) session holds it; the session ends it and retries on its next start")
                if svc != "running":
                    lines.append(f"  the session is not running: `ttp bridge setup {box}` starts it")
                elif not ag["up"]:
                    lines.append("  the session runs but the box has no live link yet; if the log says 'no forwarded "
                                 "agent', the login key's authorized_keys line forbids agent forwarding: log in with a key "
                                 "that may forward (login.identity)")
                elif ag["keys"] == 0:
                    lines.append("  the link answers but the agent holds no keys: load the key into it on this computer")
                for t in tail:
                    lines.append(f"  log: {t[:200]}")
    return lines, 1 if bad else 0
