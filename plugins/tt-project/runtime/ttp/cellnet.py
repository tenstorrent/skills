# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The outside half of a bench cell's network (cellns.py): the cell's agent and every command it runs live in
their own empty network namespace (loopback only, no route to anything the host listens on). What the agent is
meant to reach crosses a unix socket in the cell's folder, made by a forwarder out here and picked up by the
in-cell relay (cellnet_relay.py):
- UnixForward: the run's model bridge (chat bridge, claude CLI bridge): its TCP port on the host's loopback;
- ConnectProxy: for an arm whose own CLI calls its provider's API (not through a bridge), an HTTP CONNECT proxy that
  allows only port 443 of the provider's API domains, resolves the name itself and refuses loopback, private,
  link-local and IP-literal targets. The model calls of every other arm are made out here, never in the cell.
Nothing here names a host port or machine: ports come from the run, domains from the provider."""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
from pathlib import Path

# The API domains of a provider whose CLI calls it directly from the cell (a bridge arm needs none).
API_DOMAINS = {"claude": ("anthropic.com", "claude.ai"), "codex": ("openai.com", "chatgpt.com"),
               "cursor": ("cursor.sh", "cursor.com", "cursorapi.com")}
IN_CELL_PROXY_PORT = 3128   # the cell's own loopback: the relay listens here and HTTPS_PROXY names it
HEADER_MAX = 8192
CONNECT_S = 15.0


def pump(a: socket.socket, b: socket.socket) -> None:
    """Copy `a` to `b` until either ends, then close both."""
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def bridge_pair(a: socket.socket, b: socket.socket) -> None:
    t = threading.Thread(target=pump, args=(b, a), daemon=True)
    t.start()
    pump(a, b)
    t.join(2)
    a.close()
    b.close()


class UnixServer:
    """A unix socket listener at `path`; `handle(conn)` runs on its own thread per connection."""

    def __init__(self, path: Path | str, handle):
        self.path, self.handle = str(path), handle
        self.sock: socket.socket | None = None

    def start(self) -> "UnixServer":
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(64)
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    def _accept(self) -> None:
        while self.sock is not None:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        try:
            self.handle(conn)
        except Exception:
            conn.close()

    def stop(self) -> None:
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


def forward(path: Path | str, host: str, port: int) -> UnixServer:
    """Every connection to the unix socket `path` goes to host:port (the run's bridge, on loopback here)."""
    def handle(conn: socket.socket) -> None:
        up = socket.create_connection((host, port), timeout=CONNECT_S)
        up.settimeout(None)
        bridge_pair(conn, up)
    return UnixServer(path, handle).start()


def allowed(host: str, domains: tuple) -> bool:
    """Whether `host` (a name, never an IP literal) is one of `domains` or a subdomain of one."""
    host = host.strip().rstrip(".").lower()
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def public_address(host: str, port: int) -> str | None:
    """The first address `host` resolves to when every one is public, else None (any loopback, private,
    link-local, multicast, reserved or unspecified answer refuses the name: a name that points at the host's own
    services is not an API)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    addrs = [i[4][0] for i in infos]
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return None
    return addrs[0] if addrs else None


def connect_proxy(path: Path | str, domains: tuple) -> UnixServer:
    """An HTTP CONNECT proxy on the unix socket `path` for `domains` on port 443 only (see the module doc)."""
    def deny(conn: socket.socket, code: str) -> None:
        try:
            conn.sendall(f"HTTP/1.1 {code}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode())
        finally:
            conn.close()

    def handle(conn: socket.socket) -> None:
        conn.settimeout(CONNECT_S)
        head = b""
        while b"\r\n\r\n" not in head and len(head) < HEADER_MAX:
            chunk = conn.recv(4096)
            if not chunk:
                return conn.close()
            head += chunk
        line = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(line) < 2 or line[0].upper() != "CONNECT":
            return deny(conn, "405 Method Not Allowed")
        host, _, port = line[1].rpartition(":")
        host = host.strip("[]")
        if port != "443" or not allowed(host, domains):
            return deny(conn, "403 Forbidden")
        addr = public_address(host, 443)
        if addr is None:
            return deny(conn, "403 Forbidden")
        try:
            up = socket.create_connection((addr, 443), timeout=CONNECT_S)
        except OSError:
            return deny(conn, "502 Bad Gateway")
        conn.settimeout(None)
        up.settimeout(None)
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        rest = head.split(b"\r\n\r\n", 1)[1]
        if rest:
            up.sendall(rest)
        bridge_pair(conn, up)
    return UnixServer(path, handle).start()
