# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The interface relay of `ttp bridge` (standard library only: the service runs a copy of this file).

Some hosts are reachable from this computer only over one interface (a VPN that adds no route and
is used through ssh's BindInterface, say). A reverse forward cannot bind its connections, so the
session forwards such a host's box port to a unix socket here instead, and this relay connects
each connection to that host's port 22 bound to the interface. Nothing else is carried.

usage: bridge_relay.py <socket dir> <name>=<host>%<interface> ... -- <ssh argv...>

It listens on <socket dir>/<name>.sock (mode 600, the directory 700), runs the ssh session, and
exits with it, removing its sockets: the relay lives exactly as long as the session. It exits 4
before listening when the socket directory is not a private one of this account (its own, 700,
not a symlink).
"""
from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
import threading

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


def main(argv: list[str]) -> int:
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
    child = subprocess.Popen(ssh)

    def end(signum, _frame):
        child.terminate()

    signal.signal(signal.SIGTERM, end)
    signal.signal(signal.SIGINT, end)
    try:
        return child.wait()
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
