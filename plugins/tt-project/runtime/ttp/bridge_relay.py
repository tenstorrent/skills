# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The interface relay of `ttp bridge` (standard library only: the service runs a copy of this file).

Some hosts are reachable from this computer only over one interface (a VPN that adds no route and
is used through ssh's BindInterface, say). A reverse forward cannot bind its connections, so the
session forwards such a host's box port to a unix socket here instead, and this relay connects
each connection to that host's port 22 bound to the interface. Nothing else is carried.

usage: bridge_relay.py [--tag <box>/<agent>] <socket dir> [<name>=<host>%<interface> ...] -- <ssh argv...>

It listens on <socket dir>/<name>.sock (mode 600, the directory 700), runs the ssh session, and
exits with it, removing its sockets: the relay lives exactly as long as the session. It exits 4
before listening when the socket directory is not a private one of this account (its own, 700,
not a symlink).

With --tag it also recovers from a stale session. Each session's remote command carries a tag,
`ttp-bridge-tag=<computer id>/<box>/<agent>/<session id>` (the computer id is random, made on first
use and kept next to the socket dir; the session id is new each run). When a network change leaves
the box's sshd holding an earlier session's reverse-forward ports, ssh exits 255 with 'remote port
forwarding failed'. The relay then runs one cleanup login to the box (the same login options, no
forwards, no agent, a short timeout) that ends only sshd processes of the box account itself that
listen on 127.0.0.1 at one of this session's forward ports and whose session carries this
computer's tag for the same box and agent (this computer's session for them has just failed, so
such a session has no live peer here): SIGTERM, then SIGKILL after 5 s. It never uses sudo and
leaves untagged, interactive and other accounts' sessions alone. The session then starts again at
once; a second forward failure in a row exits nonzero, so the service's own restart pacing applies.

When the box-side loop finds this session's forwarded agent dead (exit 5: two failed `ssh-add -l`
in a row, 30 s apart), the session reconnects after 5, 20, then 60 s; a fourth dead agent within
10 min of the last exits, so the service's restart pacing applies.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

IP_BOUND_IF = 25      # macOS <netinet/in.h>
IPV6_BOUND_IF = 125   # macOS <netinet6/in6.h>
SO_BINDTODEVICE = 25  # Linux <asm-generic/socket.h>


def parse_spec(spec: str) -> tuple[str, str, str]:
    name, _, rest = spec.partition("=")
    host, _, iface = rest.rpartition("%")
    if not name or not host or not iface or "/" in name:
        raise ValueError(f"relay spec {spec!r}: <name>=<host>%<interface>")
    return name, host, iface


def bound_connect(host: str, iface: str, port: int = 22, timeout: float = 15) -> socket.socket:
    """A TCP connection to host:port leaving through iface."""
    err: OSError = OSError(f"{host}: no address")
    for fam, typ, proto, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        s = socket.socket(fam, typ, proto)
        try:
            if sys.platform == "darwin":
                idx = socket.if_nametoindex(iface)
                if fam == socket.AF_INET6:
                    s.setsockopt(socket.IPPROTO_IPV6, IPV6_BOUND_IF, idx)
                else:
                    s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, idx)
            else:
                s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode())
            s.settimeout(timeout)
            s.connect(addr)
            s.settimeout(None)
            return s
        except OSError as e:
            err = e
            s.close()
    raise err


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(conn: socket.socket, host: str, iface: str) -> None:
    try:
        up = bound_connect(host, iface)
    except OSError as e:
        print(f"ttp bridge relay: {host} over {iface}: {e}", file=sys.stderr, flush=True)
        conn.close()
        return
    t = threading.Thread(target=_pump, args=(up, conn), daemon=True)
    t.start()
    _pump(conn, up)
    t.join()
    conn.close()
    up.close()


def private_dir(d: str) -> bool:
    """Fail closed: the socket directory is a real directory of this account that only it can open."""
    try:
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return False
        if st.st_mode & 0o077:
            os.chmod(d, 0o700)
            st = os.lstat(d)
    except OSError:
        return False
    return st.st_mode & 0o777 == 0o700


def listen(path: str, host: str, iface: str) -> socket.socket:
    if os.path.lexists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        srv.bind(path)
    finally:
        os.umask(old)
    srv.listen(16)

    def accept() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=_handle, args=(conn, host, iface), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return srv


FORWARD_FAILED = "remote port forwarding failed"
TAG = "ttp-bridge-tag="
CLEAN_TIMEOUT = 45
STEADY_S = 60   # a session that stayed up this long starts a fresh count of forward failures
AGENT_DEAD = 5  # the box-side loop's exit when this session's forwarded agent stopped answering (bridge.py)
AGENT_RETRY_S = (5, 20, 60)   # reconnects after a dead agent, then the service's own restart pacing
AGENT_STEADY_S = 600   # a session that stayed up this long starts a fresh count of dead-agent reconnects

# On the box (POSIX sh; ss, else /proc): $1 the tag prefix, then the ports. Prints `ended <port>
# <pid> <age s>` per session it ends and `kept <port> <pid> <why>` per listener it leaves.
CLEANUP = r"""tag=$1; shift; me=$(id -u)
listeners() {
  if command -v ss >/dev/null 2>&1; then
    ss -Hltnp 2>/dev/null | awk -v a="127.0.0.1:$1" '$4 == a' | grep -o 'pid=[0-9]*' | cut -d= -f2
  else
    hex=$(printf '0100007F:%04X' "$1")
    for ino in $(awk -v a="$hex" '$2 == a && $4 == "0A" {print $10}' /proc/net/tcp); do
      for f in /proc/[0-9]*/fd/*; do
        [ "$(readlink "$f" 2>/dev/null)" = "socket:[$ino]" ] && { p=${f#/proc/}; echo "${p%%/*}"; }
      done
    done
  fi
}
alive() { kill -0 "$1" 2>/dev/null && ! ps -o stat= -p "$1" 2>/dev/null | grep -q Z; }   # a zombie has ended
for port in "$@"; do
  for pid in $(listeners "$port" | sort -u); do
    [ "$(stat -c %u "/proc/$pid" 2>/dev/null)" = "$me" ] || { echo "kept $port $pid another account"; continue; }
    case "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" in sshd*) ;; *) echo "kept $port $pid not an sshd session"; continue;; esac
    ps -o ppid=,args= -e 2>/dev/null | awk -v p="$pid" '$1 == p' | grep -qF -- "$tag" \
      || { echo "kept $port $pid not this computer's session"; continue; }
    age=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
    kill -TERM "$pid" 2>/dev/null; n=0
    while alive "$pid" && [ $n -lt 5 ]; do sleep 1; n=$((n+1)); done
    alive "$pid" && kill -KILL "$pid" 2>/dev/null
    echo "ended $port $pid ${age:-?}"
  done
done"""


def computer_id(d: str) -> str:
    """This computer's random bridge id, made on first use (never a host name)."""
    path = os.path.join(os.path.dirname(os.path.abspath(d)), "computer-id")
    try:
        with open(path) as f:
            cid = f.read().strip()
        if re.fullmatch(r"[0-9a-f]{12}", cid):
            return cid
    except OSError:
        pass
    cid = secrets.token_hex(6)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(cid + "\n")
    os.replace(path + ".tmp", path)
    return cid


def forward_ports(ssh: list[str]) -> list[int]:
    return [int(m.group(1)) for a, b in zip(ssh, ssh[1:]) if a == "-R"
            for m in [re.match(r"127\.0\.0\.1:(\d+):", b)] if m]


def tagged(ssh: list[str], tag: str) -> list[str]:
    """The session with its tag at the head of its remote command (a no-op `:` the box can read)."""
    return ssh[:-1] + [f": {TAG}{tag}; " + ssh[-1]]


def cleanup_argv(ssh: list[str], prefix: str, ports: list[int]) -> list[str]:
    """One login to the same box with the session's own login options: no forwards, no agent, a short timeout."""
    cut = ssh.index("--")
    out, skip = [], False
    for i, a in enumerate(ssh[:cut]):
        if skip:
            skip = False
            continue
        nxt = ssh[i + 1] if i + 1 < cut else ""
        if a == "-R" or (a == "-o" and re.match(r"(ForwardAgent|ExitOnForwardFailure|ConnectTimeout)=", nxt)):
            skip = True
            continue
        out.append(a)
    script = "sh -c " + shlex.quote(CLEANUP) + " ttp-bridge-clean " + " ".join(
        shlex.quote(x) for x in [TAG + prefix, *map(str, ports)])
    return out + ["-a", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10",
                  "--", ssh[cut + 1], script]


def clean(ssh: list[str], prefix: str, ports: list[int]) -> None:
    box = ssh[ssh.index("--") + 1]
    try:
        r = subprocess.run(cleanup_argv(ssh, prefix, ports), stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=CLEAN_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"ttp bridge: {box}: the stale-session cleanup did not finish ({e})", file=sys.stderr, flush=True)
        return
    ended = 0
    for ln in r.stdout.splitlines():
        w = ln.split()
        if w[:1] == ["ended"] and len(w) == 4:
            ended += 1
            print(f"ttp bridge: {box}: ended a stale session holding port {w[1]} (pid {w[2]}, {w[3]} s old)",
                  file=sys.stderr, flush=True)
        elif w[:1] == ["kept"] and len(w) >= 3:
            print(f"ttp bridge: {box}: port {w[1]} is held by pid {w[2]} ({' '.join(w[3:])}): left alone",
                  file=sys.stderr, flush=True)
    if r.returncode or not ended:
        print(f"ttp bridge: {box}: the cleanup ended no session (exit {r.returncode}"
              f"{': ' + r.stderr.strip()[-200:] if r.stderr.strip() else ''})", file=sys.stderr, flush=True)


def session(ssh: list[str], state: dict) -> tuple[int, bool]:
    """Run one session, passing its stderr through; (exit code, whether a forward failed)."""
    child = subprocess.Popen(ssh, stderr=subprocess.PIPE)
    state["child"] = child
    failed = False
    for raw in child.stderr:
        sys.stderr.buffer.write(raw)
        sys.stderr.flush()
        failed = failed or FORWARD_FAILED in raw.decode(errors="replace")
    return child.wait(), failed


def wait(seconds: float, state: dict) -> None:
    """Sleep, ending early once the service is stopped."""
    end = time.monotonic() + seconds
    while not state["stop"] and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def main(argv: list[str]) -> int:
    tag = ""
    if argv[:1] == ["--tag"] and len(argv) > 1:
        tag, argv = argv[1], argv[2:]
    if "--" not in argv or len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    cut = argv.index("--")
    d, specs, ssh = argv[0], argv[1:cut], argv[cut + 1:]
    os.makedirs(d, mode=0o700, exist_ok=True)
    if not private_dir(d):
        print(f"bridge_relay: {d} is not a private directory of this account (its own, 700, not a symlink)",
              file=sys.stderr)
        return 4
    paths = []
    for spec in specs:
        name, host, iface = parse_spec(spec)
        path = os.path.join(d, name + ".sock")
        listen(path, host, iface)
        paths.append(path)
    state: dict = {"stop": False, "child": None}

    def end(signum, _frame):
        state["stop"] = True
        if state["child"] is not None:
            state["child"].terminate()

    signal.signal(signal.SIGTERM, end)
    signal.signal(signal.SIGINT, end)
    try:
        if not tag:
            state["child"] = subprocess.Popen(ssh)
            return state["child"].wait()
        prefix, ports, cleaned, dead = f"{computer_id(d)}/{tag}/", forward_ports(ssh), False, 0
        while True:
            t0 = time.monotonic()
            rc, failed = session(tagged(ssh, prefix + secrets.token_hex(4)), state)
            if time.monotonic() - t0 >= STEADY_S:
                cleaned = False
            if time.monotonic() - t0 >= AGENT_STEADY_S:
                dead = 0
            if rc == AGENT_DEAD and not state["stop"]:
                if dead >= len(AGENT_RETRY_S):
                    print("ttp bridge: the forwarded agent died again after each reconnect; exiting (the service "
                          "restarts the session)", file=sys.stderr, flush=True)
                    return rc
                print(f"ttp bridge: the forwarded agent stopped answering; reconnecting in {AGENT_RETRY_S[dead]}s",
                      file=sys.stderr, flush=True)
                wait(AGENT_RETRY_S[dead], state)
                dead += 1
                continue
            if state["stop"] or rc != 255 or not failed or not ports:
                return rc
            if cleaned:
                print("ttp bridge: a reverse forward failed again right after the cleanup; exiting",
                      file=sys.stderr, flush=True)
                return rc
            clean(ssh, prefix, ports)
            cleaned = True
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
