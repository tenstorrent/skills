# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The in-cell half of a bench cell's network (cellnet.py): standalone (run with `python3 -I -S` inside the cell's
empty network namespace; imports only the standard library). It listens on the cell's own loopback and passes each
connection to a unix socket in the cell's folder, then runs the agent as its child and ends with its exit code.

    cellnet_relay.py [--bridge-sock PATH] [--proxy-sock PATH --proxy-port N] -- argv...

--bridge-sock: listen on 127.0.0.1:$TTP_RUN_PROXY_PORT (the run's bridge port, set by the runner) and pass
connections on to PATH. --proxy-sock/--proxy-port: the same for a provider-API proxy. Without a socket the
agent still starts, with loopback and nothing else."""
import os
import signal
import socket
import subprocess
import sys
import threading


def pump(a, b):
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


def serve_one(conn, path):
    try:
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        up.connect(path)
    except OSError:
        conn.close()
        return
    t = threading.Thread(target=pump, args=(up, conn), daemon=True)
    t.start()
    pump(conn, up)
    t.join(2)
    conn.close()
    up.close()


def listen(port, path):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(64)

    def accept():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=serve_one, args=(conn, path), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()


def main(argv):
    bridge = proxy = ""
    proxy_port = 0
    while argv and argv[0] != "--":
        flag = argv.pop(0)
        value = argv.pop(0) if argv else ""
        if flag == "--bridge-sock":
            bridge = value
        elif flag == "--proxy-sock":
            proxy = value
        elif flag == "--proxy-port":
            proxy_port = int(value)
    cmd = argv[1:]
    if not cmd:
        print("cellnet_relay: no command", file=sys.stderr)
        return 2
    try:
        port = os.environ.get("TTP_RUN_PROXY_PORT", "")
        if bridge and port.isdigit():
            listen(int(port), bridge)
        if proxy and proxy_port:
            listen(proxy_port, proxy)
    except OSError as e:
        print(f"cellnet_relay: {e}", file=sys.stderr)
        return 126
    try:
        child = subprocess.Popen(cmd)
    except OSError as e:
        print(f"cellnet_relay: {cmd[0]}: {e.strerror}", file=sys.stderr)
        return 127 if isinstance(e, FileNotFoundError) else 126

    def pass_on(sig, _frame):
        try:
            child.send_signal(sig)
        except OSError:
            pass
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, pass_on)
    while True:
        try:
            code = child.wait()
        except KeyboardInterrupt:
            continue
        return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
