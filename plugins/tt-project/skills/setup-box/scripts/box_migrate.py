"""`box.py migrate check`: what a machine still depends on from file syncing (devsync, mutagen)
and an older sync-based workflow repo. Read-only: it reports, it changes nothing. Steps:
`migrate-off-sync.md`.

Exit 0 when nothing active is left, 1 when something is (commented-out cron lines, dangling
skill links and loops whose directory holds a DONE or STOP marker count as inactive), 2 when the
check could not run.
"""

from __future__ import annotations

import json
import os
import pathlib
import plistlib
import re
import shutil
import socket
import subprocess
from typing import Dict, Iterable, List, Optional, Tuple

SKILL_DIRS = (".claude/skills", ".agents/skills", ".cursor/skills")
HOOK_FILES = (".claude/settings.json", ".codex/hooks.json", ".cursor/hooks.json")
DEVRUN_RE = re.compile(r"\bdev(run|lock)\.sh\b")
PATH_RE = re.compile(r"(?:~|\$HOME|/)[^\s'\"`;|&<>()]*")
SCRIPT_SUFFIXES = {"", ".sh", ".bash", ".zsh", ".py", ".mk"}
SCRIPT_NAMES = {"Makefile", "justfile", "Justfile"}
TTW_KEYS_RE = re.compile(r"^\s*(device_host|remote_root)\s*=\s*(.+?)\s*$", re.M)
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3]
OWN_TESTS_RE = re.compile(r"^tests/test_(tt_project_\w+|plugin_catalogue)\.py$")
LOOP_MARKERS = ("DONE", "STOP")  # a loop directory holding one of these no longer does anything
SYSTEMD_KEYS_RE = re.compile(r"^\s*(ExecStart\w*|WorkingDirectory)\s*=\s*(.+?)\s*$", re.M)


def _expand(p: str, home: pathlib.Path) -> pathlib.Path:
    if p.startswith("$HOME"):
        p = str(home) + p[len("$HOME"):]
    elif p.startswith("~"):
        p = str(home) + p[1:]
    return pathlib.Path(os.path.normpath(p))


def ttw_root(path: pathlib.Path) -> Optional[pathlib.Path]:
    """The workflow clone `path` lies in, or None.

    A clone is a directory holding scripts/devrun.sh next to skills/, under any name."""
    for d in (path, *path.parents):
        try:
            if (d / "scripts" / "devrun.sh").is_file() and (d / "skills").is_dir():
                return d
        except OSError:
            pass
    return None


def _paths_in(text: str, home: pathlib.Path) -> Iterable[pathlib.Path]:
    for m in PATH_RE.finditer(text):
        if len(m.group(0)) > 1:
            yield _expand(m.group(0), home)


def _ttw_path_in(text: str, home: pathlib.Path) -> Optional[pathlib.Path]:
    for p in _paths_in(text, home):
        if ttw_root(p):
            return p
    return None


def _add(found: list, kind: str, detail: str, active: bool = True, **extra) -> None:
    found.append({"kind": kind, "active": active, "detail": detail, **extra})


def check_devsync(home: pathlib.Path, found: list, site: Optional[str] = None) -> None:
    units = []
    agents = home / "Library" / "LaunchAgents"
    if agents.is_dir():
        units += [p for p in sorted(agents.iterdir()) if "devsync" in p.name]
    for p in units:
        _add(found, "devsync-unit", str(p))
    sd = home / ".config" / "systemd" / "user"
    if sd.is_dir():
        files = [p for p in sorted(sd.rglob("*")) if "devsync" in p.name and not p.is_dir()]
        wanted = {p.name for p in files if p.parent.name.endswith((".wants", ".requires"))}
        for p in files:
            if p.parent.name.endswith((".wants", ".requires")):
                continue
            enabled, running = p.name in wanted, _unit_active(p.name)
            if enabled or running:
                why = ", ".join(w for w, on in (("enabled", enabled), ("running", running)) if on)
                _add(found, "devsync-unit", str(p), note=why)
            else:
                _add(found, "devsync-unit", str(p), active=False, note="disabled and stopped")
    cfg = home / ".ssh" / "config"
    try:
        lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for n, line in enumerate(lines, 1):
        s = line.strip()
        body = s.lstrip("#").strip()
        if body.lower().startswith("include") and "devsync" in body and "ssh-hosts" in body:
            active = not s.startswith("#")
            _add(found, "ssh-include", f"{cfg}:{n}: {s}", active=active)
            if active:
                for inc in body.split()[1:]:
                    path = _expand(inc, home)
                    _live_aliases(path if path.is_absolute() else home / ".ssh" / path, found, site)


def _ssh_hosts(path: pathlib.Path) -> List[Tuple[str, str]]:
    """(Host, HostName) of each concrete Host block in an ssh config file."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    hosts: List[List[str]] = []
    cur: List[List[str]] = []
    for line in lines:
        key, _, val = re.sub(r"\s*=\s*|\s+", " ", line.strip(), count=1).partition(" ")
        key = key.lower()
        if key in ("host", "match"):
            cur = [[h, h] for h in val.split() if key == "host" and not any(c in h for c in "*?!")]
            hosts += cur
        elif key == "hostname":
            for h in cur:
                h[1] = val.strip()
    return [(a, b) for a, b in hosts]


def live_reservations(site: Optional[str]) -> Tuple[Dict[str, str], str]:
    """{machine: reservation id} from `ird list` on each IRD box's management host in the site
    file (read-only; only ird.user's rows when the box names one), and why that list is
    incomplete ("" when it is not)."""
    import box
    import box_ird
    try:
        boxes = box.load_site(site).get("boxes")
    except (box.SiteError, AttributeError):
        return {}, ""  # no site file: nothing to compare against
    irds = [b["ird"] for b in (boxes.values() if isinstance(boxes, dict) else [])
            if isinstance(b, dict) and b.get("route") == "ird" and isinstance(b.get("ird"), dict)]
    live: Dict[str, str] = {}
    why = []
    for mgmt, user in dict.fromkeys((i.get("mgmt"), i.get("user")) for i in irds if i.get("mgmt")):
        r = box.ssh_run(mgmt, ["ird", "list", "--noheader"], timeout=60, opts=("-n",))
        if r.returncode:
            why.append(f"`ird list` on {mgmt} exit {r.returncode}")
            continue
        for row in box_ird.parse_list(r.stdout):
            if not user or row["user"] == user:
                live[row["machine"]] = row["id"]
    return live, "; ".join(why)


def _live_aliases(include: pathlib.Path, found: list, site: Optional[str]) -> None:
    """Warn for each Host in devsync's include that a live reservation still needs: removing the
    Include drops the alias while the reservation runs."""
    hosts = _ssh_hosts(include)
    if not hosts:
        return
    live, why = live_reservations(site)
    if why:
        _add(found, "ssh-include", f"{include}: could not check its hosts for live reservations ({why})")
    from box_ird import same_machine as same
    for alias, hostname in hosts:
        hit = next(((m, rid) for m, rid in live.items() if same(m, hostname) or same(m, alias)), None)
        if hit:
            _add(found, "ssh-include", f"{include}: Host {alias} -> live reservation {hit[1]} on {hit[0]}",
                 note="copy this Host block into ~/.ssh/config as a plain alias before removing the Include")


def _unit_active(name: str) -> bool:
    """Read-only `systemctl --user is-active`; no systemctl or no answer counts as not running."""
    exe = shutil.which("systemctl")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "--user", "is-active", "--quiet", name], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def check_mutagen(found: list) -> None:
    exe = shutil.which("mutagen")
    if not exe:
        return
    # Never start a daemon just to look: with autostart off, no daemon means no live sessions.
    env = dict(os.environ, MUTAGEN_DISABLE_AUTOSTART="1")
    try:
        r = subprocess.run([exe, "sync", "list"], capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        _add(found, "mutagen", f"`mutagen sync list` did not answer: {e}")
        return
    if r.returncode != 0:
        if "daemon" in (r.stderr + r.stdout).lower():
            return
        _add(found, "mutagen", f"`mutagen sync list` exit {r.returncode}: "
             f"{(r.stderr or r.stdout).strip()[:200]}")
        return
    name = None
    for line in r.stdout.splitlines():
        key, _, val = line.strip().partition(":")
        if key == "Name":
            name = val.strip()
        elif key == "Identifier":
            _add(found, "mutagen", f"sync session {name or '?'} ({val.strip()})")
            name = None


def check_skill_links(home: pathlib.Path, found: list) -> None:
    for rel in SKILL_DIRS:
        d = home / rel
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_symlink():
                continue
            target = _expand(str(p.parent / os.readlink(p)), home)
            if ttw_root(target):
                _add(found, "skill-link", f"{p} -> {target}", active=target.exists(),
                     note="" if target.exists() else "dangling")


def _inert(paths: Iterable[pathlib.Path]) -> str:
    """The DONE or STOP marker in a loop directory among `paths`, or ""."""
    for p in paths:
        for m in LOOP_MARKERS:
            try:
                if (p / m).is_file():
                    return str(p / m)
            except OSError:
                pass
    return ""


def _add_loop(found: list, kind: str, detail: str, text: str, home: pathlib.Path, active: bool,
              note: str = "") -> None:
    marker = _inert(p for p in _paths_in(text, home) if p.is_dir())
    if marker and active:
        _add(found, kind, detail, active=False, inert=True, note=f"inert: {marker}")
    else:
        _add(found, kind, detail, active=active, **({"note": note} if note else {}))


def _plist_command(p: pathlib.Path) -> str:
    try:
        with p.open("rb") as f:
            d = plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException):
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    if not isinstance(d, dict):
        return ""
    args = d.get("ProgramArguments") if isinstance(d.get("ProgramArguments"), list) else []
    parts = [d.get("Program"), *args, d.get("WorkingDirectory")]
    return " ".join(str(x) for x in parts if isinstance(x, str))


def check_agents(home: pathlib.Path, found: list) -> None:
    """launchd agents (macOS) and systemd user units (Linux) that run a script in a workflow
    clone, e.g. a loop agent running <clone>/scripts/cron_lap.sh. devsync's own are check_devsync's."""
    agents = home / "Library" / "LaunchAgents"
    if agents.is_dir():
        for p in sorted(agents.glob("*.plist")):
            text = _plist_command(p)
            if "devsync" not in p.name and _ttw_path_in(text, home):
                _add_loop(found, "agent", f"{p}: {text[:160]}", text, home, active=True)
    sd = home / ".config" / "systemd" / "user"
    if not sd.is_dir():
        return
    wanted = {p.name for p in sd.glob("*.wants/*")} | {p.name for p in sd.glob("*.requires/*")}
    for p in sorted(x for x in sd.iterdir() if x.is_file() and "devsync" not in x.name):
        try:
            text = " ".join(v for _, v in SYSTEMD_KEYS_RE.findall(p.read_text(encoding="utf-8",
                                                                              errors="replace")))
        except OSError:
            continue
        if not _ttw_path_in(text, home):
            continue
        timer = p.with_suffix(".timer").name
        on = [w for w, hit in (("enabled", p.name in wanted or timer in wanted),
                               ("running", _unit_active(p.name) or _unit_active(timer))) if hit]
        _add_loop(found, "agent", f"{p}: {text[:160]}", text, home, active=bool(on),
                  note=", ".join(on) if on else "disabled and stopped")


def _commands(node, where=()):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "command" and isinstance(v, str):
                yield where, v
            else:
                yield from _commands(v, where + (k,) if isinstance(v, (dict, list)) else where)
    elif isinstance(node, list):
        for v in node:
            yield from _commands(v, where)


def check_hooks(home: pathlib.Path, found: list) -> None:
    for rel in HOOK_FILES:
        f = home / rel
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            _add(found, "hook", f"{f}: unreadable ({e}); check its hooks by hand")
            continue
        for where, cmd in _commands(data.get("hooks", {}) if isinstance(data, dict) else {}):
            p = _ttw_path_in(cmd, home)
            if not p:
                continue
            event = next((w for w in where if not w.isdigit()), "?")
            _add(found, "hook", f"{f} {event}: {cmd}",
                 note="" if p.exists() else "script missing: errors on every matching tool call")


def check_cron(home: pathlib.Path, found: list) -> None:
    exe = shutil.which("crontab")
    if not exe:
        return
    try:
        r = subprocess.run([exe, "-l"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return
    if r.returncode != 0:
        return
    for n, line in enumerate(r.stdout.splitlines(), 1):
        s = line.strip()
        if s and _ttw_path_in(s, home):
            _add_loop(found, "cron", f"crontab line {n}: {s[:160]}", s, home, active=not s.startswith("#"))


def _git(repo: pathlib.Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode in (0, 1) else None


def check_repo(repo: pathlib.Path, found: list) -> None:
    if not repo.is_dir() or ttw_root(repo.resolve()):
        return
    tomls = [repo / "ttw.toml"]
    listed = _git(repo, "ls-files", "--", "ttw.toml", "*/ttw.toml")
    tomls += [repo / p for p in (listed or "").splitlines()]
    for t in dict.fromkeys(tomls):
        try:
            text = t.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for key, val in TTW_KEYS_RE.findall(text):
            _add(found, "ttw-toml", f"{t}: {key} = {val}")
    hits = _git(repo, "grep", "-l", "-I", "-E", r"dev(run|lock)\.sh")
    if hits is None:
        hits = "\n".join(str(p.relative_to(repo)) for p in _walk_text(repo) if _mentions_devrun(p))
    for rel in hits.splitlines():
        p = (repo / rel).resolve()
        if rel and _is_script(p) and not ttw_root(p) and not _is_own(repo, rel, p):
            _add(found, "devrun-caller", str(repo / rel))


def _is_own(repo: pathlib.Path, rel: str, p: pathlib.Path) -> bool:
    """tt-project's own files (this script, its guide, its tests) name devrun.sh but call nothing."""
    rel = rel.replace(os.sep, "/")
    if p.is_relative_to(PLUGIN_ROOT) or rel.startswith("plugins/tt-project/"):
        return True
    return bool(OWN_TESTS_RE.match(rel)) and (repo / "plugins" / "tt-project").is_dir()


def _is_script(p: pathlib.Path) -> bool:
    """Scripts only: logs and run transcripts that quote devrun.sh call nothing."""
    return p.suffix in SCRIPT_SUFFIXES or p.name in SCRIPT_NAMES


def _walk_text(repo: pathlib.Path, limit: int = 20000):
    n = 0
    for dirpath, dirnames, files in os.walk(repo):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "build")]
        for f in files:
            n += 1
            if n > limit:
                return
            yield pathlib.Path(dirpath) / f


def _mentions_devrun(p: pathlib.Path) -> bool:
    try:
        if p.stat().st_size > 1_000_000:
            return False
        return bool(DEVRUN_RE.search(p.read_text(encoding="utf-8", errors="strict")))
    except (OSError, UnicodeDecodeError):
        return False


def this_host() -> str:
    """This machine's short name, as tt-project records it in the registry."""
    raw = os.environ.get("TTP_HOST") or socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9._-]", "", raw) or "localhost"


def default_repos(home: pathlib.Path, skipped: Optional[list] = None) -> List[pathlib.Path]:
    """The current git checkout plus every project the tt-project registry knows on this machine.

    A registered project on another host goes to `skipped` as (dir, host); one with no host whose
    directory is not here is left out silently."""
    repos = []
    top = _git(pathlib.Path.cwd(), "rev-parse", "--show-toplevel")
    if top and top.strip():
        repos.append(pathlib.Path(top.strip()))
    here = this_host().lower()
    try:
        reg = json.loads((home / ".tt-project" / "registry.json").read_text(encoding="utf-8"))
        for p in (reg.get("projects") or {}).values():
            if not (isinstance(p, dict) and isinstance(p.get("dir"), str)):
                continue
            host = p.get("host") if isinstance(p.get("host"), str) else ""
            if host and host.lower() != here:
                if skipped is not None:
                    skipped.append((p["dir"], host))
            elif host or pathlib.Path(p["dir"]).is_dir():
                repos.append(pathlib.Path(p["dir"]))
    except (OSError, ValueError, AttributeError):
        pass
    return list(dict.fromkeys(repos))


def migrate_check(home: pathlib.Path, repos: List[pathlib.Path], site: Optional[str] = None) -> list:
    found: list = []
    check_devsync(home, found, site)
    check_mutagen(found)
    check_skill_links(home, found)
    check_hooks(home, found)
    check_agents(home, found)
    check_cron(home, found)
    for repo in repos:
        check_repo(repo, found)
    return found


KINDS = ("devsync-unit", "ssh-include", "mutagen", "skill-link", "hook", "agent", "cron", "ttw-toml",
         "devrun-caller")


def cmd_migrate_check(args) -> int:
    home = pathlib.Path(args.home or os.path.expanduser("~"))
    if not home.is_dir():
        print(f"error: no home directory {home}")
        return 2
    skipped: list = []
    repos = [pathlib.Path(r).expanduser() for r in args.repo] if args.repo else default_repos(home, skipped)
    found = migrate_check(home, repos, getattr(args, "site", None))
    active = sum(f["active"] for f in found)
    if args.json:
        print(json.dumps({"active": active, "found": found, "repos": [str(r) for r in repos],
                          "skipped": [{"dir": d, "host": h} for d, h in skipped]}, indent=2))
        return 1 if active else 0
    for kind in KINDS:
        items = [f for f in found if f["kind"] == kind]
        if not items:
            print(f"clear {kind}")
        for f in items:
            note = f" ({f['note']})" if f.get("note") else ""
            label = "found" if f["active"] else "inert" if f.get("inert") else "inactive"
            print(f"{label} {kind}: {f['detail']}{note}")
    print(f"{active} active leftover(s); repos scanned: {', '.join(map(str, repos)) or 'none'}")
    for d, h in skipped:
        print(f"skipped: {d} on {h}")
    print("report only: nothing was changed (steps: migrate-off-sync.md)")
    return 1 if active else 0


def register(sub) -> None:
    mig = sub.add_parser("migrate", help="moving off file syncing and a sync-based workflow repo")
    msub = mig.add_subparsers(dest="migrate_cmd", required=True)
    c = msub.add_parser("check", help="report what this machine still depends on (read-only)")
    c.add_argument("--home", help="home directory to inspect (default: yours)")
    c.add_argument("--repo", action="append", default=[],
                   help="project checkout to scan for ttw.toml and devrun.sh/devlock.sh callers "
                        "(repeatable; default: this checkout and the tt-project registry's projects)")
    c.add_argument("--json", action="store_true", help="machine-readable output")
    c.set_defaults(func=cmd_migrate_check)
