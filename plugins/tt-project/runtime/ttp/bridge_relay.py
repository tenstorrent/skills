# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The interface relay of `ttp bridge` (standard library only: the service runs a copy of this file).

Some hosts are reachable from this computer only over one interface (a VPN that adds no route and
is used through ssh's BindInterface, say). A reverse forward cannot bind its connections, so the
session forwards such a host's box port to a unix socket here instead, and this relay connects
each connection to that host's port 22 bound to the interface. Nothing else is carried.

usage: bridge_relay.py [--tag <box>/<agent>] [--link <~/agent link>] [--keep-awake] <socket dir> [<name>=<host>%<interface> ...] -- <ssh argv...>

It listens on <socket dir>/<name>.sock (mode 600, the directory 700), runs the ssh session, and
exits with it, removing its sockets: the relay lives exactly as long as the session. It exits 4
before listening when the socket directory is not a private one of this account (its own, 700,
not a symlink).

With --tag it also recovers from a stale session. Each session's remote command carries a tag,
`ttp-bridge-tag=<computer id>/<box>/<agent>/<session id>` (the computer id is random, made on first
use and kept next to the socket dir; the session id is new each run). When a network change leaves
the box's sshd holding an earlier session's reverse-forward ports, ssh exits 255 with 'remote port
forwarding failed'. The relay then runs one cleanup login to the box (the same login options, no
forwards, no agent, a short timeout) that ends only sshd processes of the box account itself (by
their real uid, as ps reads it), never its own session or an ancestor of it, found three ways:
- the agent link (--link, the box path bridge.py links the forwarded agent to): it points at
  `.../agent.<pid>`, the socket of the sshd session that made it; that sshd is ended when its socket
  is still there and its agent no longer answers (a session whose computer slept or moved: sshd
  keeps its reverse-forward ports, and hides them from ss, as long as the dead connection lasts);
- a listener on 127.0.0.1 at one of this session's forward ports, when its session carries this
  computer's tag for the same box and agent, or its session command already ended;
- a session record: each session's remote command writes one first,
  `${XDG_CACHE_HOME:-~/.cache}/tt-project/bridge-<computer id>+<box>+<agent>+<session id>.pid` (its
  sshd's pid and its own, mode 600), and removes it when it exits; the cleanup reads and removes this
  computer's records for the box and agent, and ends a recorded sshd only while it carries the tag.
Each is sent SIGTERM; SIGKILL follows after 5 s only while it lives and a forward port is still held.
The cleanup then re-checks the ports: it succeeds only once they are free, and otherwise names each
held port and its owner (when this account can see it). It never uses sudo and leaves untagged
interactive sessions, live agents and other accounts' sessions alone. A cleanup that left a port
held runs again after 5, 20 and 60 s before the session reconnects, then the relay exits nonzero so
the service's own restart pacing applies. Once the ports are free the session starts again at once;
a second forward failure in a row exits nonzero.

With --keep-awake (macOS only; the service passes it unless the bridge config says `"keep_awake":
"off"`) it also holds `/usr/bin/caffeinate -s -w <its pid>` for as long as it runs: PreventSystemSleep,
which macOS honours only on AC power, so the computer stays awake on the charger, nothing is held on
battery and the display is never kept awake (no -d, -i, -u or -m). `-w` ends the assertion with the
relay, and the relay ends it itself on exit, so a restart never leaves two. Any other platform spawns nothing.

When the box-side loop finds this session's forwarded agent dead (exit 5: two failed `ssh-add -l`
in a row, 30 s apart), the session reconnects after 5, 20, then 60 s; a fourth dead agent within
10 min of the last exits, so the service's restart pacing applies.

ssh runs in a process group of its own, and the relay never relies on it exiting by itself: open
forwarded-agent channels can keep it up long after its remote command ended, holding the local
agent's connections. The remote command reports its own end on stderr (`ttp-bridge-end <exit>`,
from its EXIT trap); an ssh still up 3 s later is ended with its whole group (SIGTERM, SIGKILL 5 s
later) and the relay goes on as if ssh had exited with that code. With --tag the relay also asks
the local agent the session forwards for its keys every 30 s (`ssh-add -l`, 10 s timeout): a
timeout or a failed exchange, confirmed by one more check, logs one line saying why, ends the
group and reconnects like a dead agent above. A stop (SIGTERM, SIGINT) ends the group the same way.
"""
from __future__ import annotations

import errno
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
TCP_KEEPALIVE_DARWIN = 0x10   # macOS <netinet/tcp.h>: the idle time before the first probe
KEEPALIVE = (60, 15, 4)   # idle s, probe interval s, probes: a vanished peer is reset in about 2 min
DRAIN_S = 30.0   # once one direction of a carried connection ends, the other gets this long
ACCEPT_RETRY = {errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM, errno.ECONNABORTED, errno.EINTR}
ACCEPT_BACKOFF_MAX_S = 1.0
SUPERVISE_S = 30.0   # how often each listener is checked
RELISTEN_TRIES = 2   # failed re-listens in a row before the relay exits for a restart
RELAY_LOST = 6   # the exit when a listener cannot be restored
NOFILE_WANT = 4096
CONNECT_DEADLINE_S = 15.0   # one upstream connect, name lookup and every address together
MAX_CARRIED = 64   # carried connections per listener; more are closed at once while these last
MAX_CARRIED_ALL = 512   # in the whole relay, lowered to fit its open-file limit (fit_carried)
FD_RESERVE = 64   # descriptors kept free for listeners, logs and the session


def parse_spec(spec: str) -> tuple[str, str, str]:
    name, _, rest = spec.partition("=")
    host, _, iface = rest.rpartition("%")
    if not name or not host or not iface or "/" in name:
        raise ValueError(f"relay spec {spec!r}: <name>=<host>%<interface>")
    return name, host, iface


def _resolve(host: str, port: int, timeout: float) -> list:
    """getaddrinfo within timeout: a lookup stuck on a dead network is left to finish on its own."""
    out: dict = {}

    def look():
        try:
            out["addrs"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as e:
            out["err"] = e

    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(max(0.0, timeout))
    if "err" in out:
        raise out["err"]
    if "addrs" not in out:
        raise TimeoutError(f"{host}: name lookup timed out")
    return out["addrs"]


def bound_connect(host: str, iface: str, port: int = 22, timeout: float | None = None) -> socket.socket:
    """A TCP connection to host:port leaving through iface, all within one deadline (CONNECT_DEADLINE_S)."""
    deadline = time.monotonic() + (CONNECT_DEADLINE_S if timeout is None else timeout)
    err: OSError = OSError(f"{host}: no address")
    for fam, typ, proto, _, addr in _resolve(host, port, deadline - time.monotonic()):
        left = deadline - time.monotonic()
        if left <= 0:
            err = TimeoutError(f"{host}: connect timed out")
            break
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
            s.settimeout(left)
            s.connect(addr)
            s.settimeout(None)
            keepalive(s)
            return s
        except OSError as e:
            err = e
            s.close()
    raise err


def keepalive(s: socket.socket, platform: str | None = None) -> None:
    """Short TCP keepalive probes, each option only where the platform has it (never fails)."""
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None)
    if idle_opt is None and (platform or sys.platform) == "darwin":
        idle_opt = getattr(socket, "TCP_KEEPALIVE", TCP_KEEPALIVE_DARWIN)
    idle, intvl, cnt = KEEPALIVE
    for level, opt, val in ((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                            (socket.IPPROTO_TCP, idle_opt, idle),
                            (socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPINTVL", None), intvl),
                            (socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPCNT", None), cnt)):
        if opt is None:
            continue
        try:
            s.setsockopt(level, opt, val)
        except OSError:
            pass


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


def _close(s: socket.socket | None) -> None:
    """Shut down, then close: a shutdown also wakes a recv blocked on it in another thread."""
    if s is None:
        return
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


def _handle(conn: socket.socket, host: str, iface: str) -> None:
    up = None
    try:
        try:
            up = bound_connect(host, iface)
        except OSError as e:
            print(f"ttp bridge relay: {host} over {iface}: {e}", file=sys.stderr, flush=True)
            return
        ended = threading.Event()

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                _pump(src, dst)
            finally:
                ended.set()

        ts = [threading.Thread(target=pump, args=pair, daemon=True) for pair in ((up, conn), (conn, up))]
        for t in ts:
            t.start()
        ended.wait()
        deadline = time.monotonic() + DRAIN_S
        for t in ts:
            t.join(max(0.0, deadline - time.monotonic()))
    finally:
        _close(conn)
        _close(up)


class Gate:
    """A count of carried connections with a cap."""

    def __init__(self, cap: int):
        self.cap, self.n, self.lock = cap, 0, threading.Lock()

    def take(self) -> bool:
        with self.lock:
            if self.n >= self.cap:
                return False
            self.n += 1
            return True

    def give(self) -> None:
        with self.lock:
            self.n = max(0, self.n - 1)


ALL = Gate(MAX_CARRIED_ALL)   # every listener's carried connections together


def fit_carried(soft: int | None) -> int:
    """Size the relay-wide cap to the open-file limit: two descriptors per carried connection, FD_RESERVE kept."""
    if soft is not None and soft > 0:
        ALL.cap = max(8, min(MAX_CARRIED_ALL, (soft - FD_RESERVE) // 2))
    return ALL.cap


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


class Listener:
    """One relay socket: <path> carried to host:22 over iface, kept listening (see supervise)."""

    def __init__(self, path: str, host: str, iface: str, sock=socket.socket):
        self.path, self.host, self.iface, self.sock = path, host, iface, sock
        self.srv: socket.socket | None = None
        self.ino = 0
        self.thread: threading.Thread | None = None
        self.closing = False
        self.gate = Gate(MAX_CARRIED)
        self.refusing = False

    def start(self) -> None:
        if os.path.lexists(self.path):
            os.unlink(self.path)
        srv = self.sock(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old = os.umask(0o177)
            try:
                srv.bind(self.path)
            finally:
                os.umask(old)
            srv.listen(16)
            self.ino = os.lstat(self.path).st_ino
        except OSError:
            srv.close()
            raise
        self.srv = srv
        self.thread = threading.Thread(target=self._accept, args=(srv,), daemon=True)
        self.thread.start()

    def _accept(self, srv: socket.socket) -> None:
        delay = 0.0
        while True:
            try:
                conn, _ = srv.accept()
            except OSError as e:
                if self.closing or srv is not self.srv or srv.fileno() == -1:
                    return   # closed on purpose, or replaced by a re-listen
                if e.errno not in ACCEPT_RETRY:
                    print(f"ttp bridge relay: {self.path}: accept failed ({e}); re-listening",
                          file=sys.stderr, flush=True)
                    return   # the supervisor re-listens
                if not delay:
                    print(f"ttp bridge relay: {self.path}: accept failed ({e}); retrying",
                          file=sys.stderr, flush=True)
                delay = min(ACCEPT_BACKOFF_MAX_S, delay * 2 or 0.05)
                time.sleep(delay)
                continue
            delay = 0.0
            if not self.gate.take():
                self._refuse(conn, f"{self.gate.n} connections open (cap {self.gate.cap})")
                continue
            if not ALL.take():
                self.gate.give()
                self._refuse(conn, f"{ALL.n} connections open in the relay (cap {ALL.cap})")
                continue
            self.refusing = False
            try:
                threading.Thread(target=self._carry, args=(conn,), daemon=True).start()
            except RuntimeError as e:   # no thread to carry it: refuse this one, keep listening
                print(f"ttp bridge relay: {self.path}: {e}", file=sys.stderr, flush=True)
                _close(conn)
                self.gate.give()
                ALL.give()

    def _refuse(self, conn: socket.socket, why: str) -> None:
        """Close a connection over a cap at once; one log line per burst."""
        _close(conn)
        if not self.refusing:
            self.refusing = True
            print(f"ttp bridge relay: {self.path}: {why}; closing new ones until some end",
                  file=sys.stderr, flush=True)

    def _carry(self, conn: socket.socket) -> None:
        try:
            _handle(conn, self.host, self.iface)
        finally:
            self.gate.give()
            ALL.give()

    def problem(self) -> str:
        """Why this listener no longer takes connections, or ''."""
        if self.srv is None or self.srv.fileno() == -1:
            return "its socket is closed"
        if self.thread is None or not self.thread.is_alive():
            return "its accept loop ended"
        try:
            st = os.lstat(self.path)
        except OSError:
            return "its socket file is gone"
        if not stat.S_ISSOCK(st.st_mode) or st.st_ino != self.ino:
            return "its socket file was replaced"
        return ""

    def close(self, unlink: bool = False) -> None:
        self.closing = unlink or self.closing
        srv, self.srv = self.srv, None
        _close(srv)
        if unlink:
            try:
                if os.lstat(self.path).st_ino == self.ino:
                    os.unlink(self.path)
            except OSError:
                pass


def listen(path: str, host: str, iface: str, sock=socket.socket) -> Listener:
    lst = Listener(path, host, iface, sock)
    lst.start()
    return lst


def supervise_once(listeners: list[Listener], fails: dict) -> bool:
    """Check each listener and re-listen a broken one; False once one failed RELISTEN_TRIES re-listens in a row."""
    for lst in listeners:
        why = lst.problem()
        if not why:
            fails.pop(lst.path, None)
            continue
        lst.close()
        try:
            lst.start()
        except OSError as e:
            fails[lst.path] = fails.get(lst.path, 0) + 1
            print(f"ttp bridge relay: {lst.path}: {why}; re-listening failed ({e})", file=sys.stderr, flush=True)
            if fails[lst.path] >= RELISTEN_TRIES:
                return False
            continue
        fails.pop(lst.path, None)
        print(f"ttp bridge relay: {lst.path}: {why}; listening again", file=sys.stderr, flush=True)
    return True


def supervise(listeners: list[Listener], stop: threading.Event, lost) -> None:
    """Every SUPERVISE_S until stop: restore broken listeners; call lost() when one cannot be restored."""
    fails: dict = {}
    while not stop.wait(SUPERVISE_S):
        if not supervise_once(listeners, fails):
            print(f"ttp bridge relay: a listener could not be restored {RELISTEN_TRIES} times in a row; exiting "
                  "(the service restarts the relay)", file=sys.stderr, flush=True)
            lost()
            return


def raise_nofile(want: int = NOFILE_WANT, rl=None) -> int | None:
    """Raise the soft open-file limit to min(hard, want); the soft limit after, None where unknown. Never fails."""
    try:
        if rl is None:
            import resource as rl
        soft, hard = rl.getrlimit(rl.RLIMIT_NOFILE)
    except (ImportError, OSError, ValueError):
        return None
    target = want if hard == rl.RLIM_INFINITY else min(hard, want)
    if soft != rl.RLIM_INFINITY and soft < target:
        try:
            rl.setrlimit(rl.RLIMIT_NOFILE, (target, hard))
            return target
        except (OSError, ValueError):
            pass
    return soft


FORWARD_FAILED = "remote port forwarding failed"
TAG = "ttp-bridge-tag="
CLEAN_TIMEOUT = 45
STEADY_S = 60   # a session that stayed up this long starts a fresh count of forward failures
AGENT_DEAD = 5  # the box-side loop's exit when this session's forwarded agent stopped answering (bridge.py)
AGENT_RETRY_S = (5, 20, 60)   # reconnects after a dead agent, then the service's own restart pacing
AGENT_STEADY_S = 600   # a session that stayed up this long starts a fresh count of dead-agent reconnects
END = re.compile(r"^ttp-bridge-end (\d+)\s*$")   # the remote command's own last line (tagged's EXIT trap)
END_GRACE_S = 3.0      # once the remote command ended, ssh gets this long to exit before its group is ended
CLEAN_RETRY_S = (5, 20, 60)   # cleanups again while the ports stay held, then the service's own restart pacing
KILL_GRACE_S = 5.0     # SIGTERM to ssh's process group, then SIGKILL this long after
LOCAL_CHECK_S = 30.0   # how often the local agent the session forwards is asked for its keys
LOCAL_TIMEOUT_S = 10.0   # an `ssh-add -l` that takes longer is a hung agent
LOCAL_RECHECK_S = 2.0  # a failed check is confirmed by one more this soon after
ENV_AGENT = "TTP_BRIDGE_AGENT"   # bridge.py's name for the service's own agent in its ssh wrapper

# `ok <socket>`: the agent there answers (ssh-add -l exits 0, or 1 with no keys); 2, a timeout or a
# failed exchange is a dead agent. A session whose workstation agent died keeps its socket file, so
# `-S` alone never notices.
AGENT_OK = ("ok() { t=; command -v timeout >/dev/null 2>&1 && t='timeout 10'; "
            'o=$(SSH_AUTH_SOCK="$1" $t ssh-add -l 2>&1); r=$?; [ $r -eq 0 ] || { [ $r -eq 1 ] && '
            "! printf '%s' \"$o\" | grep -qiE 'error fetching|communication with agent'; }; }")

# On the box (POSIX sh; ss, else /proc): $1 the tag prefix, $2 the record name prefix, $3 the agent
# link (a '~/' path, or empty), then the ports. Prints `ended <port> <pid> <age s>` per session it ends,
# `kept <port> <pid> <why>` per one it leaves and, last, `held <port> <pid or ?>` per port still held;
# <port> is `link` or `rec` for a session found by the agent link or its record. A port whose listener's
# process is hidden (sshd sessions are often unreadable to their own account) is matched to one of the
# account's sshd sessions by pausing each for a moment (hidden()); the usual checks then decide.
CLEANUP = r"""tag=$1; rec=$2; link=$3; shift 3; me=$(id -u); ports="$*"; seen=
case $link in '~/'*) link=$HOME/${link#'~/'};; esac
mine=$$; p=$$   # this cleanup's own shell, its sshd and their ancestors are never ended
while p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ') && [ -n "$p" ] && [ "$p" -gt 1 ]; do mine="$mine $p"; done
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
held() {   # the port has a listener, whether or not this account can see its process
  if command -v ss >/dev/null 2>&1; then
    ss -Hltn 2>/dev/null | awk -v a="127.0.0.1:$1" '$4 == a {f = 1} END {exit !f}'
  else
    awk -v a="$(printf '0100007F:%04X' "$1")" '$2 == a && $4 == "0A" {f = 1} END {exit !f}' /proc/net/tcp
  fi
}
any_held() { for q in $ports; do held "$q" && return 0; done; return 1; }
poke() {   # one connection to the port, closed at once
  if command -v nc >/dev/null 2>&1; then nc -z -w 2 127.0.0.1 "$1" >/dev/null 2>&1
  elif command -v bash >/dev/null 2>&1; then timeout 3 bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$0"' "$1" >/dev/null 2>&1
  else return 1; fi
}
queued() { ss -Hltn 2>/dev/null | awk -v a="127.0.0.1:$1" '$4 == a {print $2; exit}'; }   # its unaccepted connections
paused=; trap '[ -z "$paused" ] || kill -CONT "$paused" 2>/dev/null' EXIT HUP INT TERM
owns() {   # $1 port, $2 pid: paused, the pid leaves a connection to the port unaccepted
  paused=$2; kill -STOP "$2" 2>/dev/null || { paused=; return 1; }
  poke "$1"; q=$(queued "$1"); kill -CONT "$2" 2>/dev/null; paused=
  [ "${q:-0}" -gt 0 ]
}
hidden() {   # $1 port held by a listener whose process this account cannot see (a privilege-separated sshd):
  # the account's own sshd session that, paused for a moment, leaves a connection to it unaccepted while
  # running it accepts one at once; twice, so a slow accept elsewhere never names the wrong session
  command -v ss >/dev/null 2>&1 || return 0
  if [ -n "${TTP_TEST_SSHD_PIDS+x}" ]; then cands=$TTP_TEST_SSHD_PIDS
  else cands=$(ps -u "$me" -o pid=,comm= 2>/dev/null | awk '$2 == "sshd" || $2 == "sshd-session" {print $1}'); fi
  for c in $cands; do
    case " $mine " in *" $c "*) continue;; esac
    owns "$1" "$c" || continue
    poke "$1"; sleep 1; [ "$(queued "$1")" = 0 ] && owns "$1" "$c" && { echo "$c"; return; }
  done
}
alive() { kill -0 "$1" 2>/dev/null && case "$(ps -o stat= -p "$1" 2>/dev/null)" in *Z*) false;; esac; }   # a zombie has ended
""" + AGENT_OK + r"""
end_one() {   # $1 the port (or link, rec), $2 the pid
  case " $seen " in *" $2 "*) return;; esac; seen="$seen $2"
  case " $mine " in *" $2 "*) echo "kept $1 $2 the cleanup's own session"; return;; esac
  [ "$(ps -o uid= -p "$2" 2>/dev/null | tr -d ' ')" = "$me" ] || { echo "kept $1 $2 another account"; return; }
  case "$(tr '\0' ' ' < "/proc/$2/cmdline" 2>/dev/null)" in sshd*) ;; *) echo "kept $1 $2 not an sshd session"; return;; esac
  if [ "$1" != link ]; then   # this computer's tagged session; at a port, also one whose session command ended
    ps -o ppid=,args= -e 2>/dev/null | awk -v p="$2" -v t="$tag" -v r="$1" \
      '$1 == p {k = 1; if (index($0, t)) f = 1} END {exit !(f || (!k && r != "rec"))}' \
      || { echo "kept $1 $2 not this computer's session"; return; }
  fi
  age=$(ps -o etimes= -p "$2" 2>/dev/null | tr -d ' ')
  kill -TERM "$2" 2>/dev/null; n=0
  while alive "$2" && [ $n -lt 5 ]; do sleep 1; n=$((n+1)); done
  if alive "$2" && ! any_held; then echo "kept $1 $2 still running after SIGTERM, with the ports free"; return; fi
  alive "$2" && kill -KILL "$2" 2>/dev/null
  echo "ended $1 $2 ${age:-?}"
}
if [ -n "$link" ] && [ -L "$link" ]; then   # the agent link of the stale session: <dir>/agent.<its sshd pid>
  sock=$(readlink "$link" 2>/dev/null); pid=${sock##*/agent.}
  case "$pid" in ''|*[!0-9]*) ;; *)
    if ! [ -S "$sock" ] || case "$(ps -o stat= -p "$pid" 2>/dev/null)" in ''|*Z*) true;; *) false;; esac; then :
    elif ! command -v ssh-add >/dev/null 2>&1; then echo "kept link $pid no ssh-add to ask its agent"
    elif ok "$link"; then echo "kept link $pid its agent answers"
    else end_one link "$pid"; fi;;
  esac
fi
for port in $ports; do
  pids=$(listeners "$port" | sort -u)
  [ -n "$pids" ] || ! held "$port" || pids=$(hidden "$port")
  for pid in $pids; do end_one "$port" "$pid"; done
done
for f in "${XDG_CACHE_HOME:-$HOME/.cache}/tt-project/$rec"*.pid; do
  [ -f "$f" ] && [ ! -L "$f" ] || continue
  pid=; read -r pid _ < "$f" 2>/dev/null
  case "$pid" in ''|*[!0-9]*) ;; *) case "$(ps -o stat= -p "$pid" 2>/dev/null)" in ''|*Z*) ;; *) end_one rec "$pid";; esac;; esac
  rm -f "$f"
done
n=0; while any_held && [ $n -lt 3 ]; do sleep 1; n=$((n+1)); done   # a listener ended a moment ago
for port in $ports; do
  held "$port" && echo "held $port $(listeners "$port" | head -n 1 | grep . || echo '?')"
done
exit 0"""


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


def rec_name(tag: str) -> str:
    """The box-side record name of a session tag (or the prefix of a tag prefix): bridge-<cid>+<box>+<agent>+<sid>."""
    return "bridge-" + "+".join(re.sub(r"[^A-Za-z0-9._-]", "_", p) for p in tag.split("/"))


def tagged(ssh: list[str], tag: str) -> list[str]:
    """The session with its tag at the head of its remote command (a no-op `:` the box can read), then
    the session record: its sshd's pid and its own, written private and removed when it exits; its last
    stderr line names its exit code, so the relay can end an ssh that stays up after it."""
    rec = '"${XDG_CACHE_HOME:-$HOME/.cache}/tt-project/"' + shlex.quote(rec_name(tag) + ".pid")
    return ssh[:-1] + [f": {TAG}{tag}; ttp_rec={rec}; (umask 077; mkdir -p \"${{ttp_rec%/*}}\" && "
                       f'echo "$PPID $$" > "$ttp_rec") 2>/dev/null; '
                       f'trap \'ttp_rc=$?; rm -f "$ttp_rec"; echo "ttp-bridge-end $ttp_rc" >&2\' EXIT; ' + ssh[-1]]


def cleanup_argv(ssh: list[str], prefix: str, ports: list[int], link: str = "") -> list[str]:
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
        shlex.quote(x) for x in [TAG + prefix, rec_name(prefix), link, *map(str, ports)])
    return out + ["-a", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10",
                  "--", ssh[cut + 1], script]


def clean(ssh: list[str], prefix: str, ports: list[int], link: str = "") -> bool:
    """One cleanup login; True once the box reports the session's ports free."""
    box = ssh[ssh.index("--") + 1]

    def say(m: str) -> None:
        print(f"ttp bridge: {box}: {m}", file=sys.stderr, flush=True)

    try:
        r = subprocess.run(cleanup_argv(ssh, prefix, ports, link), stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=CLEAN_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        say(f"the stale-session cleanup did not finish ({e})")
        return False
    ended, held = 0, []
    for ln in r.stdout.splitlines():
        w = ln.split()
        what = {"rec": "found by its record", "link": "found by its agent link"}.get(
            w[1] if len(w) > 1 else "", f"holding port {w[1] if len(w) > 1 else '?'}")
        if w[:1] == ["ended"] and len(w) == 4:
            ended += 1
            say(f"ended a stale session {what} (pid {w[2]}, {w[3]} s old)")
        elif w[:1] == ["kept"] and len(w) >= 3:
            say(f"a session {what} (pid {w[2]}) is left alone: {' '.join(w[3:])}")
        elif w[:1] == ["held"] and len(w) == 3:
            held.append(w[1])
            say(f"port {w[1]} is still held, " + (f"by pid {w[2]}" if w[2] != "?" else
                "by a process this account cannot see (an sshd session no record or agent link names)"))
    if r.returncode:
        say(f"the cleanup login failed (exit {r.returncode}{': ' + r.stderr.strip()[-200:] if r.stderr.strip() else ''})")
        return False
    if not ended and not held:
        say("the cleanup ended no session; the ports are free")
    return not held


def forwarded_agent(ssh: list[str], env=None) -> str:
    """The local agent socket the session forwards ('' for none): ssh's first ForwardAgent option, a
    path, `$VAR`/`${VAR}` (bridge.py's own name stands for the service's SSH_AUTH_SOCK) or `yes`."""
    env = os.environ if env is None else env
    opts = ssh[:ssh.index("--")] if "--" in ssh else ssh
    val = next((b.split("=", 1)[1].strip().strip('"') for a, b in zip(opts, opts[1:])
                if a == "-o" and b.lower().startswith("forwardagent=")), "")
    if val.lower() == "yes":
        return env.get("SSH_AUTH_SOCK", "")
    m = re.fullmatch(r"\$\{?(\w+)\}?", val)
    if m:
        return env.get(m.group(1)) or (env.get("SSH_AUTH_SOCK", "") if m.group(1) == ENV_AGENT else "")
    return "" if val.lower() in ("", "no") else os.path.expanduser(val)


def agent_fault(sock: str) -> str | None:
    """Why the agent at `sock` does not answer ('' when it does, None when ssh-add cannot run here):
    `ssh-add -l` exits 0, or 1 with no keys; 2, a timeout or a failed exchange is a dead agent."""
    try:
        r = subprocess.run(["ssh-add", "-l"], env=dict(os.environ, SSH_AUTH_SOCK=sock), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=LOCAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"ssh-add -l got no answer within {LOCAL_TIMEOUT_S:g}s"
    except OSError:
        return None
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0 or (r.returncode == 1 and not re.search(r"error fetching|communication with agent", out, re.I)):
        return ""
    return f"ssh-add -l exit {r.returncode}{': ' + out.splitlines()[-1][:120] if out else ''}"


def watch_agent(sock: str, sess: dict, done: threading.Event) -> None:
    """Every LOCAL_CHECK_S, ask the forwarded agent for its keys; two failures in a row set sess["agent"]."""
    while not done.wait(LOCAL_CHECK_S):
        why = agent_fault(sock)
        if why is None:
            return
        if why and not done.wait(LOCAL_RECHECK_S):
            why = agent_fault(sock)
            if why:
                sess["agent"] = why
                return


def signal_group(child: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        pass
    except OSError:
        if child.poll() is None:
            child.send_signal(sig)


def end_group(child: subprocess.Popen) -> None:
    """End ssh and everything in its process group: SIGTERM, then SIGKILL after KILL_GRACE_S."""
    signal_group(child, signal.SIGTERM)
    try:
        child.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    signal_group(child, signal.SIGKILL)


def pass_stderr(child: subprocess.Popen, sess: dict) -> None:
    for raw in child.stderr:
        line = raw.decode(errors="replace")
        m = END.match(line)
        if m:
            sess["end"], sess["end_t"] = int(m.group(1)), time.monotonic()
            continue
        sys.stderr.buffer.write(raw)
        sys.stderr.flush()
        sess["failed"] = sess["failed"] or FORWARD_FAILED in line


def session(ssh: list[str], state: dict) -> tuple[int, bool]:
    """Run one session, passing its stderr through; (exit code, whether a forward failed). ssh is ended
    with its group once its remote command ended (that command's exit code) or the local agent it
    forwards stopped answering (AGENT_DEAD), never left to exit by itself."""
    # own session: ssh and what it spawns are ended whole (its group) by end_group(), never left behind
    child = subprocess.Popen(ssh, stderr=subprocess.PIPE, start_new_session=True)
    state["child"] = child
    sess: dict = {"failed": False, "end": None, "end_t": 0.0, "agent": ""}
    done = threading.Event()
    reader = threading.Thread(target=pass_stderr, args=(child, sess), daemon=True)
    reader.start()
    sock = forwarded_agent(ssh)
    if sock:
        threading.Thread(target=watch_agent, args=(sock, sess, done), daemon=True).start()
    rc = None
    while rc is None:
        try:
            rc = child.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            pass
        if state["stop"]:
            end_group(child)
        elif sess["end"] is not None and time.monotonic() - sess["end_t"] >= END_GRACE_S:
            print(f"ttp bridge: the remote command ended (exit {sess['end']}) but ssh stayed up; ending it",
                  file=sys.stderr, flush=True)
            end_group(child)
            rc = sess["end"]
        elif sess["agent"]:
            print(f"ttp bridge: the local agent this session forwards stopped answering ({sess['agent']}); "
                  "ending the session", file=sys.stderr, flush=True)
            end_group(child)
            rc = AGENT_DEAD
    done.set()
    reader.join(timeout=2)   # stderr ends with the group; an escaped holder of the pipe never blocks the relay
    return rc, sess["failed"]


def wait(seconds: float, state: dict) -> None:
    """Sleep, ending early once the service is stopped."""
    end = time.monotonic() + seconds
    while not state["stop"] and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


CAFFEINATE = "/usr/bin/caffeinate"


def keep_awake_argv(pid: int) -> list[str]:
    """-s only: PreventSystemSleep is honoured on AC power only, and never the display."""
    return [CAFFEINATE, "-s", "-w", str(pid)]


def hold_awake(platform: str | None = None, spawn=None, pid: int | None = None):
    """Start the sleep assertion tied to this process (macOS only); the Popen, or None when nothing started."""
    if (platform or sys.platform) != "darwin":
        return None
    try:
        return (spawn or subprocess.Popen)(keep_awake_argv(pid or os.getpid()), stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"bridge_relay: could not keep the computer awake: {e.strerror or e}", file=sys.stderr, flush=True)
        return None


def release_awake(proc) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv: list[str], platform: str | None = None, awake_spawn=None) -> int:
    tag, keep, link = "", False, ""
    while argv[:1] in (["--tag"], ["--keep-awake"], ["--link"]):
        if argv[0] == "--keep-awake":
            keep, argv = True, argv[1:]
        elif len(argv) > 1:
            tag, link = (argv[1], link) if argv[0] == "--tag" else (tag, argv[1])
            argv = argv[2:]
        else:
            break
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
    fit_carried(raise_nofile())   # before the session starts: its ssh inherits the raised limit
    listeners: list[Listener] = []   # kept referenced for the life of the relay
    for spec in specs:
        name, host, iface = parse_spec(spec)
        listeners.append(listen(os.path.join(d, name + ".sock"), host, iface))
    state: dict = {"stop": False, "child": None, "lost": False}
    awake = hold_awake(platform, awake_spawn) if keep else None

    def end(signum, _frame):
        state["stop"] = True
        if state["child"] is not None:
            signal_group(state["child"], signal.SIGTERM)

    def lost():
        state["lost"] = True
        end(None, None)

    signal.signal(signal.SIGTERM, end)
    signal.signal(signal.SIGINT, end)
    stop = threading.Event()
    if listeners:
        threading.Thread(target=supervise, args=(listeners, stop, lost), daemon=True).start()
    try:
        rc = run(ssh, tag, d, state, link)
        return RELAY_LOST if state["lost"] else rc
    finally:
        stop.set()
        release_awake(awake)
        for lst in listeners:
            lst.close(unlink=True)


def run(ssh: list[str], tag: str, d: str, state: dict, link: str = "") -> int:
    """The session (with --tag, its stale-session and dead-agent recovery) until it ends or is stopped."""
    if not tag:
        # own session: ssh is ended whole (its group) by end_group() once stopped
        state["child"] = subprocess.Popen(ssh, start_new_session=True)
        if state["stop"]:
            end_group(state["child"])
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
        tries = 0
        while not clean(ssh, prefix, ports, link):
            if state["stop"] or tries >= len(CLEAN_RETRY_S):
                print(f"ttp bridge: the forward ports stay held after {tries + 1} cleanups; exiting (the service "
                      "restarts the session)", file=sys.stderr, flush=True)
                return rc
            print(f"ttp bridge: cleaning up again in {CLEAN_RETRY_S[tries]}s before reconnecting",
                  file=sys.stderr, flush=True)
            wait(CLEAN_RETRY_S[tries], state)
            if state["stop"]:
                return rc
            tries += 1
        cleaned = True


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
