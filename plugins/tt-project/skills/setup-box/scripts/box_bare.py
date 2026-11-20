"""setup-box bare-metal route: `box.py bare check <alias>`, read-only readiness of a box.

One ssh call reads free disk under the box's workdir root and whether the tt-device-mcp broker is
installed and its service active; locally, `ttp machines list --json` gives the box's tags, note
and disk guard, and `ttp lock --probe <alias>-device` says whether the device is free (outside a project, only a lock
`ttp machines` shares has an answer; any other is reported n/a, not as a problem).

Exit 0 ready, 1 a problem to fix (disk below the guard, broker installed but not active), 75 the
device lock is busy or paused (wait; never override it), 255 ssh failed. Usable as a retry_when.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from typing import List, Optional

import box

DEFAULT_MIN_FREE_GB = 150.0  # the harness's own disk.min_free_gb default
BROKER_CMD = "tt-device-mcp"
BROKER_SERVICE = "tt-device-broker"
LOCK_NA = 3   # `ttp lock --probe` outside a project, of a lock only a project keeps
_PLAIN = re.compile(r"[A-Za-z0-9_./${}~-]+")


def _remote_root(root: str) -> str:
    """`root` as a remote shell word: ~ and $VARS expand, anything else is quoted."""
    if root.startswith("~"):
        rest = root[1:]
        if _PLAIN.fullmatch(rest or "x"):
            root = "$HOME" + rest
        else:
            return '"$HOME"' + shlex.quote(rest)
    return f'"{root}"' if _PLAIN.fullmatch(root) else shlex.quote(root)


def probe_script(root: str, service: str = BROKER_SERVICE) -> str:
    """Read-only shell run on the box; prints key=value lines."""
    return (
        f"d={_remote_root(root)}; "
        'while [ ! -d "$d" ] && [ "$d" != / ]; do d=$(dirname "$d"); done; '
        'echo "df_path=$d"; '
        'echo "df_kb=$(df -Pk "$d" 2>/dev/null | awk \'NR==2{print $4}\')"; '
        f"if command -v {BROKER_CMD} >/dev/null 2>&1; then echo broker_cmd=yes; "
        f"if [ \"$(systemctl is-active {shlex.quote(service)} 2>/dev/null)\" = active ]; then "
        f"if command -v timeout >/dev/null 2>&1; then q=$(timeout 10 {BROKER_CMD} status 0 2>&1); "
        f"else q=$({BROKER_CMD} status 0 2>&1); fi; "
        "[ $? = 124 ] && q='status timed out'; "
        "echo \"queue=$(echo \"$q\" | head -n 6 | tr '\\n' ' ' | cut -c1-300)\"; fi; "
        "else echo broker_cmd=no; fi; "
        f"echo \"broker_svc=$(systemctl is-active {shlex.quote(service)} 2>/dev/null)\""
    )


def _local(argv: List[str], timeout: float = 30) -> Optional[subprocess.CompletedProcess]:
    """A local command, or None when it is not installed."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "timed out")


def machine_entry(alias: str) -> Optional[dict]:
    """The box's `ttp machines list` entry; None when ttp or the list cannot be read."""
    r = _local(["ttp", "machines", "list", "--json"])
    if r is None or r.returncode != 0:
        return None
    try:
        doc = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    return doc.get(alias, {}) if isinstance(doc, dict) else None


def check(alias: str, site_entry: Optional[dict], min_free_gb: Optional[float], resource: str,
          service: str = BROKER_SERVICE) -> "tuple[int, List[str], dict]":
    """(exit code, report lines, facts) for one box."""
    lines: List[str] = []
    facts: dict = {"alias": alias}
    problems: List[str] = []

    m = machine_entry(alias)
    if m is None:
        lines.append("machines: unknown (`ttp machines list` unreadable)")
    elif not m:
        lines.append("machines: not in `ttp machines list`")
    else:
        tags = list(m.get("tags") or [])
        facts["tags"] = tags
        lines.append(f"machines: tags {', '.join(tags) or 'none'}" + (f"; note: {m['note']}" if m.get("note") else ""))
        if "serving" in tags:
            lines.append("serving: it runs a service for others; the tag stops nothing (only a user-set "
                         "hold or allowlist, a lock, a pause or a live tenant does)")
        if "unstable" in tags:
            lines.append("unstable: tagged unstable; read its note before using it")
    if min_free_gb is None:
        got = (m or {}).get("min_free_gb")
        min_free_gb = float(got) if isinstance(got, (int, float)) and not isinstance(got, bool) else DEFAULT_MIN_FREE_GB
    facts["min_free_gb"] = min_free_gb

    root = (site_entry or {}).get("workdir_root") or "~"
    if site_entry and site_entry.get("route") != "bare":
        lines.append(f"site: route is {site_entry.get('route')!r}, not bare; see its page")
    r = box.ssh_run(alias, probe_script(root, service), timeout=60)
    facts["ssh_rc"] = r.returncode
    ssh_ok = r.returncode == 0
    if not ssh_ok:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        # Names the box: as a retry_when, the daemon counts a 255 as unreachable only by checking it.
        lines.append(f"ssh: connection to {alias} failed (exit {r.returncode}) {tail[0]}".rstrip())
    else:
        lines.append("ssh: ok")
        kv = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
        try:
            free_gb = int(kv.get("df_kb", "")) / 1024 ** 2
        except ValueError:
            free_gb = None
        facts.update(df_path=kv.get("df_path", ""), free_gb=free_gb)
        if free_gb is None:
            problems.append("disk")
            lines.append(f"disk: could not read free space under {root}")
        elif min_free_gb and free_gb < min_free_gb:
            problems.append("disk")
            lines.append(f"disk: {free_gb:.0f} GB free in {kv.get('df_path')}, below the guard of "
                         f"{min_free_gb:g} GB: free space or pick another box")
        else:
            lines.append(f"disk: {free_gb:.0f} GB free in {kv.get('df_path')} (guard {min_free_gb:g} GB)")
        has_cmd, svc = kv.get("broker_cmd") == "yes", kv.get("broker_svc", "").strip() or "unknown"
        facts.update(broker_cmd=has_cmd, broker_service=svc)
        queue = " ".join(kv.get("queue", "").split())
        if queue:
            facts["broker_queue"] = queue
            if "serving" in facts.get("tags", []):
                lines.append(f"broker queue (read-only, `{BROKER_CMD} status 0`): {queue}")
        if has_cmd and svc == "active":
            lines.append(f"broker: {BROKER_CMD} present, {service} active: run device work through it")
        elif has_cmd or svc == "active":
            problems.append("broker")
            lines.append(f"broker: {BROKER_CMD} {'present' if has_cmd else 'missing'}, {service} {svc}: "
                         "do not start a second broker; report it")
        else:
            lines.append(f"broker: none ({BROKER_CMD} missing, {service} {svc}); each device command "
                         f"runs as `ttp lock {resource} -- <command>`, never where TT_VISIBLE_DEVICES masks it")

    p = _local(["ttp", "lock", "--probe", resource])
    # Exit 3: outside a project, a lock no `ttp machines` share makes machine-wide; nothing holds it here.
    lock = ("unknown" if p is None else "free" if p.returncode == 0 else "busy or paused" if p.returncode == 75
            else "n/a (no project here; not a shared lock)" if p.returncode == LOCK_NA else f"error (exit {p.returncode})")
    facts["lock"] = lock
    if lock == "unknown" or lock.startswith("error"):
        problems.append("lock: could not probe")
    lines.append(f"lock {resource}: {lock}" + (": wait for it, never override it" if lock == "busy or paused" else ""))

    if not ssh_ok:
        problems.append("ssh")
    # Only ssh's own 255 means unreachable; a timeout or failed remote probe is a broken check.
    rc = (255 if r.returncode == 255 else 1) if not ssh_ok else 1 if problems else 75 if lock == "busy or paused" else 0
    facts["rc"] = rc
    lines.append("verdict: " + {0: "ready", 1: "not ready: " + ", ".join(problems),
                                75: "wait for the device lock", 255: "unreachable over ssh"}[rc])
    return rc, lines, facts


def cmd_bare_check(args) -> int:
    entry = None
    try:
        entry = box.box_entry(box.load_site(args.site), args.alias)
    except box.SiteError:
        pass
    rc, lines, facts = check(args.alias, entry, args.min_free_gb, args.resource or f"{args.alias}-device",
                             args.service)
    print(json.dumps(facts) if args.json else "\n".join(lines))
    return rc


def register(sub) -> None:
    bare = sub.add_parser("bare", help="bare-metal route").add_subparsers(dest="bare_cmd", required=True)
    c = bare.add_parser("check", help="read-only: ssh, free disk, broker, device lock")
    c.add_argument("alias", help="the box's ssh and `ttp machines` alias")
    c.add_argument("--min-free-gb", type=float, dest="min_free_gb",
                   help="disk guard (default: the machine's min_free_gb, else 150; 0 = off)")
    c.add_argument("--resource", help="the device lock to probe (default <alias>-device)")
    c.add_argument("--service", default=BROKER_SERVICE, help="the broker's systemd unit")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_bare_check)
