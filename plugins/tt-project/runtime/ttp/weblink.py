# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The web app link `ttp new` and `ttp connect` end with, checked before it is given.

The check asks the web app's state endpoint through the exact link (its port and token) and needs
HTTP 200 naming this project. A tunnel or daemon that is still coming up gets a few retries with
backoff. A link that does not answer is repaired once: for a remote project the kept tunnel is
restarted, then the daemon there; for a local one the daemon (which serves the web app) is
restarted if a service keeps it. A link that still fails is never printed: the line says what is
broken instead, and the command exits UNVERIFIED.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request

from .project import FOLDER

LINK = re.compile(r"http://127\.0\.0\.1:(\d+)/#token=([0-9a-f]+)")
UNVERIFIED = 3            # exit code: the command did its work, but the web app link failed the check
WAITS = (0.5, 1, 2, 4, 8)  # seconds between tries while a new tunnel or daemon comes up (~15 s in all)


def _sleep(s: float) -> None:   # tests replace it
    time.sleep(s)


def check(url: str, name: str, timeout: float = 5) -> tuple[str, str]:
    """("", "") when the link answers 200 for project `name`, else (kind, what is wrong). kind is
    "down" (nothing answers), "token", "web" (an answer that is not this web app's) or "name"."""
    m = LINK.search(url or "")
    if not m:
        return "web", "there is no web address to check"
    port, tok = m.group(1), m.group(2)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"X-TTP-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "token", f"the web app on localhost:{port} refuses the link's token (HTTP 401)"
        return "web", f"localhost:{port} answers HTTP {e.code}, not the web app's state"
    except (urllib.error.URLError, OSError) as e:
        why = getattr(e, "reason", None) or e
        return "down", f"nothing answers on localhost:{port} ({why})"
    except ValueError:
        return "web", f"localhost:{port} answers, but not with the web app's state"
    got = ((body or {}).get("project") or {}).get("name") if isinstance(body, dict) else None
    if got != name:
        return "name", f"localhost:{port} serves project {got!r}, not {name!r}"
    return "", ""


def verify(url: str, name: str, waits=WAITS) -> tuple[str, str]:
    """check() with retries while nothing answers yet; other failures do not change by waiting."""
    kind, why = check(url, name)
    for w in waits:
        if kind != "down":
            break
        _sleep(w)
        kind, why = check(url, name)
    return kind, why


def ok_line(url: str) -> str:
    return f"web app: {url}"


def bad_line(what: str, why: str, tried: list[str]) -> str:
    done = f" Tried: {'; '.join(tried)}." if tried else ""
    return f"web app: NOT AVAILABLE ({what}): {why}.{done} No link is given until it answers."


def local(p, url: str | None, repair: bool = True) -> tuple[str, int]:
    """(line, exit code) for a project on this machine. The daemon serves the web app: when nothing
    answers and a service keeps the daemon, restart it and check again. A daemon with no service
    (`ttp stop`, --no-service) is left stopped: starting it would start spending."""
    from . import service
    kind, why = verify(url, p.name) if url else ("down", "the daemon has not opened its web app")
    tried: list[str] = []
    if kind == "down" and repair and service.installed(p):
        tried.append(f"restarted the daemon ({service.restart_service(p)})")
        url = link(p) or url   # a daemon with no port set picks a new one
        kind, why = verify(url, p.name) if url else ("down", "the daemon has not opened its web app")
    if not kind:
        return ok_line(url), 0
    if kind == "down":
        what = "daemon"
        if not service.installed(p):
            why += "; no service keeps the daemon running, so it stays stopped"
    else:
        what = kind
    return bad_line(what, why, tried), UNVERIFIED


def link(p) -> str | None:
    from .web import token
    port = (p.db.kv("web") or {}).get("port") or p.config().get("web", {}).get("port")
    return f"http://127.0.0.1:{port}/#token={token(p)}" if port else None


def _ssh_ok(host: str) -> str:
    """"" when this machine logs in to host without a prompt, else ssh's complaint."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, "true"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return "" if r.returncode == 0 else ((r.stderr or "").strip().splitlines() or [f"exit {r.returncode}"])[-1]


def remote(name: str, entry: dict, remote_port: int, tok: str) -> tuple[str, int]:
    """(line, exit code) for a project on another machine: open the kept local forward exactly as
    `ttp web <name> --tunnel --keep` does (a local view forward needs no ask), check the link
    through it, and repair: restart the kept tunnel, then the daemon there."""
    from . import tunnel
    from .web import free_port
    host = entry.get("ssh") or entry["host"]
    local_port, did = tunnel.keep(name, host, remote_port, free_port)
    url = f"http://127.0.0.1:{local_port}/#token={tok}"
    kind, why = verify(url, name)
    tried: list[str] = []
    if kind == "down":
        tried.append(f"restarted the kept tunnel ({tunnel.restart(name)})")
        kind, why = verify(url, name)
    if kind == "down":
        cant = _ssh_ok(host)
        if cant:
            return bad_line("tunnel", f"{why}; ssh to {host} fails without a prompt ({cant})", tried), UNVERIFIED
        remote_ttp = f"{entry['dir']}/{FOLDER}/harness/bin/ttp"
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host,
                            f"{remote_ttp} restart {shlex.quote(name)}"], capture_output=True, text=True)
        tried.append(f"restarted the daemon on {host} ({(r.stdout or r.stderr).strip()[-160:] or r.returncode})")
        kind, why = verify(url, name)
        if kind == "down":
            return bad_line("daemon", f"{why}; ssh to {host} works, so its daemon or web app is down", tried), UNVERIFIED
    if kind:
        return bad_line(kind, why, tried), UNVERIFIED
    return f"{did}: localhost:{local_port} → {host}:{remote_port}\n" + ok_line(url), 0
