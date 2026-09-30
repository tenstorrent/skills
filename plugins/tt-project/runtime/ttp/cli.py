# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp` — create, find, talk to and operate tt-project projects.

Every project command takes the project NAME. If the registry says the project lives on another
machine, the command is forwarded over ssh to that machine's copy of the project's own `ttp`.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets as pysecrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from . import schedule as sched
from .project import (FOLDER, NAME_RE, Project, hostname, load_registry, load_secrets, register, save_secret,
                      write_json)

RUNTIME = Path(__file__).resolve().parent.parent              # .../runtime (plugin or project copy)
PLUGIN_ROOT = RUNTIME.parent                                   # plugin root, or a project's harness/
MARKER = "tt-project://{name}@{host}:{dir}"


def die(msg: str, code: int = 2) -> None:
    print(f"ttp: {msg}", file=sys.stderr)
    sys.exit(code)


# locating projects --------------------------------------------------------------------------------
def local_project(name: str) -> Project | None:
    entry = load_registry().get("projects", {}).get(name)
    if entry and entry.get("host") in (None, hostname()) and Path(entry["dir"]).is_dir():
        return Project(entry["dir"])
    cur = Path.cwd()
    for d in [cur, *cur.parents]:
        cand = Project(d)
        if cand.exists() and cand.name == name:
            return cand
    return None


def remote_entry(name: str) -> dict | None:
    entry = load_registry().get("projects", {}).get(name)
    if entry and entry.get("host") and entry["host"] != hostname():
        return entry
    return None


def forward(entry: dict, argv: list[str]) -> int:
    """Run this same command on the project's machine, streaming its output."""
    remote_ttp = f"{entry['dir']}/{FOLDER}/harness/bin/ttp"
    cmd = " ".join(shlex.quote(a) for a in [remote_ttp, *argv])
    host = entry.get("ssh") or entry["host"]
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, cmd], stderr=subprocess.PIPE,
                       text=True)
    if r.returncode == 255:   # ssh itself failed: the project is fine, this machine cannot reach it
        why = (r.stderr.strip().splitlines() or ["unknown ssh error"])[-1]
        print(f"ttp: cannot reach {host} right now ({why}). The project keeps running there; "
              f"try again once this machine is back on that network.", file=sys.stderr)
    elif r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode


def search_transcripts(name: str) -> list[dict]:
    """Find a project's location markers in agent chat logs (Claude Code, Codex, Cursor)."""
    needle = f"tt-project://{name}@"
    roots = [Path.home() / ".claude" / "projects", Path.home() / ".codex" / "sessions", Path.home() / ".cursor" / "projects"]
    hits: dict[str, dict] = {}
    rx = re.compile(re.escape(needle) + r"([A-Za-z0-9._-]+):(/[^\s\"'\\]+)")
    for root in roots:
        if not root.is_dir():
            continue
        tool = shutil.which("rg")
        cmd = [tool, "-l", "-F", needle, str(root)] if tool else ["grep", "-rlF", needle, str(root)]
        try:
            files = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.split()
        except subprocess.SubprocessError:
            continue
        for f in files:
            try:
                text = Path(f).read_text(errors="replace")
            except OSError:
                continue
            for m in rx.finditer(text):
                hits[f"{m.group(1)}:{m.group(2)}"] = {"host": m.group(1), "dir": m.group(2).rstrip(".,)"),
                                                     "seen_in": f, "mtime": Path(f).stat().st_mtime}
    return sorted(hits.values(), key=lambda h: -h["mtime"])


def resolve(name: str) -> tuple[Project | None, dict | None]:
    p = local_project(name)
    if p:
        return p, None
    entry = remote_entry(name)
    if entry:
        return None, entry
    for hit in search_transcripts(name):
        if hit["host"] == hostname() and Project(hit["dir"]).exists():
            register(name, {"host": hostname(), "dir": str(Project(hit["dir"]).root)})
            return Project(hit["dir"]), None
        if hit["host"] != hostname():
            entry = {"host": hit["host"], "dir": hit["dir"]}
            register(name, entry)
            return None, entry
    return None, None


def need(name: str, argv: list[str]) -> Project:
    p, entry = resolve(name)
    if entry:
        sys.exit(forward(entry, argv))
    if not p:
        die(f"no project named {name!r} on this machine or in the registry; try `ttp find {name}`")
    return p


# creating -----------------------------------------------------------------------------------------
def detect_provider() -> str:
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if any(k.startswith("CODEX_") for k in os.environ):
        return "codex"
    if any(k.startswith("CURSOR_") for k in os.environ):
        return "cursor"
    return "claude"


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def bootstrap(root: Path, name: str, brief: str, provider: str) -> Project:
    p = Project(root)
    if p.exists():
        if p.name != name:
            die(f"{p.base} already holds project {p.name!r}")
        return p
    p.base.mkdir(parents=True, exist_ok=True)
    # The folder ignores itself, so no enclosing repository can ever commit project state.
    (p.base / ".gitignore").write_text("*\n")
    for d in (p.state, p.runs, p.logs, p.worktrees, p.memory_dir):
        d.mkdir(parents=True, exist_ok=True)
    template = PLUGIN_ROOT / "template"
    _copy_tree(RUNTIME, p.harness / "runtime")
    _copy_tree(template / "prompts", p.harness / "prompts")
    _copy_tree(template / "bin", p.harness / "bin")
    for f in (p.harness / "bin").iterdir():
        f.chmod(0o755)
    (p.harness / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    subprocess.run(["git", "init", "-q", "-b", "upstream", str(p.harness)], check=True)
    _git(p.harness, "add", "-A")
    _git(p.harness, "-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost", "commit", "-q",
         "-m", f"tt-project template {__version__}")
    _git(p.harness, "checkout", "-q", "-b", "main")
    charter = (template / "CHARTER.md").read_text().replace("{{NAME}}", name).replace(
        "{{DATE}}", time.strftime("%Y-%m-%d")).replace("{{BRIEF}}", brief.strip() or "(no description given yet)")
    p.charter_path.write_text(charter)
    p.memory_index.write_text("# Memory index\n")
    cfg = {"name": name, "created": time.time(), "root": str(p.root), "host": hostname(),
           "core_provider": provider, "tt_project_version": __version__,
           "id": pysecrets.token_hex(4)}
    sec = load_secrets()
    if (sec.get("slack") or {}).get("bot_token"):
        cfg["notify"] = {"slack": True}
    write_json(p.config_path, cfg)
    _git(p.harness, "add", "-A")
    _git(p.harness, "-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost", "commit", "-q",
         "-m", f"project {name}: charter and config")
    db = p.db
    for s in json.loads((template / "recurring.json").read_text()):
        sched.upsert(db, s["name"], s["kind"], s["every"], s.get("at"), s.get("enabled", True),
                     s.get("budget_usd_day"), s.get("description", ""), s.get("payload", {}))
    db.set_meta("name", name)
    db.post("in", "Project created. Brief:\n" + (brief.strip() or "(none)") +
            "\n\nRead the charter, restate the goals, success criteria and restrictions as you understand "
            "them, list what you still need to know, and start the first tasks that do not depend on answers.",
            chat=None, channel="system", kind="user")
    return p


def cmd_new(a) -> None:
    if not NAME_RE.match(a.name):
        die("project names use letters, digits, '.', '_' and '-' (max 63)")
    brief = a.describe or ""
    if a.describe_file:
        brief = sys.stdin.read() if a.describe_file == "-" else Path(a.describe_file).expanduser().read_text()
    if a.host and a.host not in (hostname(), "localhost"):
        return new_remote(a, brief)
    root = Path(a.dir).expanduser().resolve() if a.dir else _default_root()
    p = bootstrap(root, a.name, brief, a.provider or detect_provider())
    register(a.name, {"host": hostname(), "dir": str(p.root)})
    ident = subprocess.run(["git", "-C", str(p.root), "config", "user.email"], capture_output=True, text=True)
    if ident.returncode != 0 or not ident.stdout.strip():
        print("warning: git has no user.email here, so workers cannot commit. Set one "
              "(git config --global user.name/user.email) or tell the coordinator which identity to use.")
    how = "service not installed (--no-service)"
    if not a.no_service:
        from .service import install
        how = install(p)
    _wait_for_daemon(p)
    print(MARKER.format(name=a.name, host=hostname(), dir=p.root))
    print(f"created {a.name} in {p.base} · daemon via {how}")
    print(web_line(p))


def _default_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(out.stdout.strip() if out.returncode == 0 else os.getcwd())


def push_secrets(host: str) -> str:
    """Copy this user's saved keys to another machine (same user), merging with what is there.
    Travels over ssh stdin, lands mode 0600, never on a command line or in a project folder."""
    sec = load_secrets()
    if not sec:
        return "no saved keys to copy"
    merge = ("import json,os,sys;h=os.path.expanduser('~/.tt-project');os.makedirs(h,mode=0o700,exist_ok=True);"
             "p=os.path.join(h,'secrets.json');old={}\n"
             "try: old=json.load(open(p))\nexcept Exception: pass\n"
             "new=json.load(sys.stdin);new.update({k:v for k,v in old.items() if k not in new});"
             "fd=os.open(p+'.tmp',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);os.write(fd,json.dumps(new).encode());"
             "os.close(fd);os.replace(p+'.tmp',p);print(','.join(sorted(new)))")
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"python3 -c {shlex.quote(merge)}"],
                       input=json.dumps(sec), text=True, capture_output=True)
    return f"copied keys ({r.stdout.strip()}) to {host}" if r.returncode == 0 else f"could not copy keys: {r.stderr[-200:]}"


def ship_runtime(host: str) -> str:
    """Copy this runtime and template to ~/.tt-project/lib/<version> on another machine and make it
    that machine's installed `ttp`. Returns the remote launcher path."""
    stage = f".tt-project/lib/{__version__}"
    subprocess.check_call(["ssh", "-o", "BatchMode=yes", host, f"rm -rf ~/{stage} && mkdir -p ~/{stage}"])
    mac = ["--no-xattrs", "--no-mac-metadata"] if sys.platform == "darwin" else []
    tar = subprocess.Popen(["tar", *mac, "--exclude", "__pycache__", "-C", str(PLUGIN_ROOT), "-czf", "-",
                            "runtime", "template", "bin"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           env={**os.environ, "COPYFILE_DISABLE": "1"})
    subprocess.check_call(["ssh", "-o", "BatchMode=yes", host, f"tar -C ~/{stage} -xzf -"], stdin=tar.stdout)
    tar.wait()
    subprocess.check_call(["ssh", "-o", "BatchMode=yes", host, f"~/{stage}/bin/ttp setup >/dev/null"])
    return f"~/{stage}/bin/ttp"


def new_remote(a, brief: str) -> None:
    """Ship this runtime to the other machine and create the project there."""
    if not a.dir:
        die("--dir is required with --host (the project root on that machine)")
    host = a.host
    stage = f".tt-project/lib/{__version__}"
    ship_runtime(host)
    if load_secrets() and not a.no_secrets:
        print(push_secrets(host))
    args = [f"~/{stage}/bin/ttp", "new", a.name, "--dir", a.dir, "--provider", a.provider or detect_provider()]
    if a.no_service:
        args.append("--no-service")
    remote = " ".join(shlex.quote(x) if not x.startswith("~/") else x for x in args) + " --describe-file -"
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, remote], input=brief, text=True)
    if r.returncode != 0:
        die(f"remote creation on {host} failed", r.returncode)
    register(a.name, {"host": host, "dir": a.dir})


def _wait_for_daemon(p: Project, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (p.db.kv("web") or {}).get("port"):
            return
        time.sleep(0.5)


def web_line(p: Project) -> str:
    from .web import token
    port = (p.db.kv("web") or {}).get("port") or p.config().get("web", {}).get("port")
    if not port:
        return "web app: not running yet (check `ttp status`)"
    return f"web app: http://127.0.0.1:{port}/#token={token(p)}"


# talking ------------------------------------------------------------------------------------------
def cmd_connect(a) -> None:
    p = need(a.name, sys.argv[1:])
    chat = a.chat or f"c{pysecrets.token_hex(3)}"
    p.db.x("INSERT INTO chats(id,created,label,host,last_active,last_read) VALUES(?,?,?,?,?,"
           "(SELECT COALESCE(MAX(id),0) FROM messages)) ON CONFLICT(id) DO UPDATE SET last_active=excluded.last_active",
           (chat, time.time(), a.label or "", os.environ.get("TTP_CLIENT_HOST", ""), time.time()))
    print(MARKER.format(name=p.name, host=hostname(), dir=p.root))
    print(f"chat: {chat}")
    print(status_text(p))
    print(web_line(p))


def cmd_say(a) -> None:
    p = need(a.name, sys.argv[1:])
    text = a.text if a.text != "-" else sys.stdin.read()
    if not text.strip():
        die("empty message")
    mid = p.db.post("in", text.strip(), chat=a.chat or None, channel="chat", kind="user")
    if a.chat:
        p.db.x("UPDATE chats SET last_active=? WHERE id=?", (time.time(), a.chat))
    print(f"sent (#{mid}); the coordinator answers in this chat when it has decided")


def cmd_listen(a) -> None:
    p = need(a.name, sys.argv[1:])
    db = p.db
    row = db.one("SELECT last_read, min_severity FROM chats WHERE id=?", (a.chat,))
    if not row:
        die(f"unknown chat {a.chat}; run `ttp connect {a.name}` first")
    after, floor = int(row["last_read"] or 0), row["min_severity"] or "normal"
    # One listener per chat: a second one would race the first for the same messages, and
    # whichever prints to nowhere would silently mark them read.
    lock = p.state / f"listen-{a.chat}.pid"
    try:
        other = int(lock.read_text())
        os.kill(other, 0)
        if other != os.getpid():
            die(f"another listener (pid {other}) is already attached to chat {a.chat}; stop it or reuse it", 3)
    except (OSError, ValueError):
        pass
    lock.write_text(str(os.getpid()))
    deadline = time.time() + a.timeout if a.timeout else None
    while True:
        msgs = db.unread_for_chat(a.chat, after, floor)
        if msgs:
            for m in msgs:
                who = "coordinator" if m["chat"] else f"{p.name} ({m['kind']}, {m['severity']})"
                print(f"[{who}] {m['text']}", flush=True)
                after = max(after, m["id"])
            db.x("UPDATE chats SET last_read=?, last_active=? WHERE id=?", (after, time.time(), a.chat))
            if a.once:
                return
        else:
            last = db.one("SELECT COALESCE(MAX(id),0) m FROM messages")["m"]
            if last > after:   # skipped messages below this chat's severity floor
                after = last
                db.x("UPDATE chats SET last_read=? WHERE id=?", (after, a.chat))
        if deadline and time.time() > deadline:
            return
        time.sleep(2)


def status_text(p: Project) -> str:
    db = p.db
    d = db.kv("daemon", {})
    from .daemon import _alive
    alive = bool(d.get("pid")) and d.get("host") == hostname() and _alive(int(d["pid"]))
    gates = db.kv("gates", {})
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    head = f"{p.name}: daemon {'running' if alive else 'NOT RUNNING'}" + (" (paused)" if db.kv("paused") else "")
    head += " · tasks: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "none yet")
    lines = [head]
    for prov, g in gates.items():
        lines.append(f"budget {prov}: {g['level']}" + (f" — {'; '.join(g['reasons'])}" if g["reasons"] else ""))
    for t in db.q("SELECT id,title,status,blocked_reason FROM tasks WHERE status IN ('running','blocked','review') "
                  "ORDER BY status, id LIMIT 12"):
        lines.append(f"  #{t['id']} {t['status']}: {t['title']}" + (f" — {t['blocked_reason']}" if t["blocked_reason"] else ""))
    for m in db.q("SELECT text FROM messages WHERE kind='ask' AND handled=0 AND ts>? ORDER BY id DESC LIMIT 5",
                  (time.time() - 14 * 86400,)):
        lines.append(f"  needs you: {m['text'][:200]}")
    return "\n".join(lines)


def cmd_status(a) -> None:
    p = need(a.name, sys.argv[1:])
    if a.json:
        from .web import state_payload
        print(json.dumps(state_payload(p, p.db), default=str, indent=1))
    else:
        print(status_text(p))


def cmd_web(a) -> None:
    p, entry = resolve(a.name)
    if p:
        print(web_line(p))
        return
    if not entry:
        die(f"no project {a.name!r}")
    host = entry.get("ssh") or entry["host"]
    remote_ttp = f"{entry['dir']}/{FOLDER}/harness/bin/ttp"
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, f"{remote_ttp} web {shlex.quote(a.name)}"],
                       capture_output=True, text=True)
    m = re.search(r"http://127\.0\.0\.1:(\d+)/#token=([0-9a-f]+)", r.stdout)
    if not m:
        die(f"could not read the web address from {host}: {(r.stderr or r.stdout).strip()[-200:]}")
    from .web import free_port
    remote_port, tok = m.group(1), m.group(2)
    local = free_port(int(remote_port) + 100)
    cmd = ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-L",
           f"{local}:127.0.0.1:{remote_port}", host]
    if a.tunnel:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        print(f"tunnel open (pid in background): localhost:{local} → {host}:{remote_port}")
    else:
        print(f"The project runs on {host}. With the user's OK, open a tunnel:\n  {' '.join(cmd)}")
    print(f"web app: http://127.0.0.1:{local}/#token={tok}")


def cmd_note(a) -> None:
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not run_dir:
        die("ttp note only works inside a tt-project run")
    with open(Path(run_dir) / "progress.md", "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {a.text}\n")


# operating ----------------------------------------------------------------------------------------
def cmd_list(a) -> None:
    reg = load_registry().get("projects", {})
    if not reg:
        print("no projects registered on this machine")
    for name, e in sorted(reg.items()):
        print(f"{name}\t{e.get('host')}:{e.get('dir')}")


def cmd_find(a) -> None:
    p, entry = resolve(a.name)
    if p:
        print(MARKER.format(name=a.name, host=hostname(), dir=p.root))
    elif entry:
        print(MARKER.format(name=a.name, host=entry["host"], dir=entry["dir"]))
    else:
        die(f"not found in the registry or in this machine's chat logs. Ask the user where it runs, "
            f"then `ttp adopt {a.name} --host HOST --dir DIR`", 1)


def cmd_adopt(a) -> None:
    register(a.name, {"host": a.host or hostname(), "dir": a.dir})
    print(MARKER.format(name=a.name, host=a.host or hostname(), dir=a.dir))


def cmd_task(a) -> None:
    p = need(a.name, sys.argv[1:])
    if a.action == "add":
        tid = p.db.add_task(a.title, a.spec or "", kind=a.kind, tier=a.tier, priority=a.priority, origin="user")
        print(f"task #{tid} queued")
    elif a.action == "list":
        for t in p.db.q("SELECT id,status,tier,title FROM tasks ORDER BY id DESC LIMIT 50"):
            print(f"#{t['id']}\t{t['status']}\t{t['tier']}\t{t['title']}")
    elif a.action == "cancel":
        p.db.update_task(int(a.title), status="cancelled")
        print("cancelled")


def cmd_memory(a) -> None:
    p = need(a.name, sys.argv[1:])
    print(p.add_memory(a.text, kind=a.kind))


def cmd_pause(a) -> None:
    p = need(a.name, sys.argv[1:])
    p.db.set_kv("paused", a.cmd == "pause")
    print(f"{p.name} {'paused: no new model runs start' if a.cmd == 'pause' else 'resumed'}")


def cmd_service(a) -> None:
    p = need(a.name, sys.argv[1:])
    from . import service
    if a.cmd == "start":
        print(service.install(p))
    elif a.cmd == "stop":
        print(service.uninstall(p))
        pid = p.state / "daemon.pid"
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), 15)
            except (OSError, ValueError):
                pass
    else:
        print(service.restart(p))


def cmd_config(a) -> None:
    p = need(a.name, sys.argv[1:])
    if a.value is None:
        node = p.config()
        for part in a.key.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else None
        print(json.dumps(node, indent=1))
        return
    try:
        val = json.loads(a.value)
    except ValueError:
        val = a.value
    p.set_config(a.key, val)
    print(f"{a.key} = {json.dumps(val)}")


def cmd_secret(a) -> None:
    if a.kind == "jev":
        key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Jev key: ")
        from .providers.jev import verify
        ok, msg = verify(a.via, key, a.url)
        if not ok and not a.force:
            die(f"the key did not work ({msg}); not saved. Use --force to save anyway.")
        save_secret("jev", {"via": a.via, "key": key, **({"url": a.url} if a.url else {})})
        print(f"saved Jev key for this user ({a.via}; check: {msg})")
    elif a.kind == "slack":
        tok = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Slack bot token (xoxb-…): ")
        from .slack import Slack
        s = Slack(tok, a.user_id, a.email)
        try:
            s.call("auth.test")
            uid = s.resolve_user()
        except Exception as e:
            die(f"Slack check failed: {e}")
        save_secret("slack", {"bot_token": tok, "user_id": uid, "user_email": a.email})
        print(f"saved Slack bot for user {uid}")
    elif a.kind == "push":
        if not a.host:
            die("--host is required")
        print(push_secrets(a.host))
    elif a.kind == "show":
        sec = load_secrets()
        print(json.dumps({k: sorted(v.keys()) if isinstance(v, dict) else "set" for k, v in sec.items()}))


def cmd_logs(a) -> None:
    p = need(a.name, sys.argv[1:])
    f = p.logs / "daemon.log"
    print(f.read_text()[-a.bytes:] if f.exists() else "(no log yet)")


def cmd_setup(a) -> None:
    """Install this runtime as the user's stable `ttp` (plugin caches move on every update)."""
    from .project import HOME_DIR
    lib = HOME_DIR / "lib" / __version__
    if RUNTIME.resolve() != (lib / "runtime").resolve():
        for part in ("runtime", "template", "bin"):
            src = PLUGIN_ROOT / part
            if src.exists():
                _copy_tree(src, lib / part)
        for f in (lib / "bin").iterdir():
            f.chmod(0o755)
    cur = HOME_DIR / "lib" / "current"
    if cur.is_symlink() or cur.exists():
        cur.unlink()
    cur.symlink_to(lib)
    bindir = Path(a.bin_dir).expanduser()
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "ttp"
    shim.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(cur / 'bin' / 'ttp'))} \"$@\"\n")
    shim.chmod(0o755)
    on_path = str(bindir) in os.environ.get("PATH", "").split(":")
    print(f"ttp {__version__} installed: {shim}" + ("" if on_path else f" (add {bindir} to PATH)"))


def cmd_upgrade(a) -> None:
    """Merge the installed template into a project's harness. The harness repo keeps pristine
    template snapshots on its `upstream` branch, so this is an ordinary three-way merge."""
    entry = remote_entry(a.name)
    if entry and not local_project(a.name):
        ship_runtime(entry.get("ssh") or entry["host"])      # the newer runtime becomes that machine's ttp
        sys.exit(forward(entry, sys.argv[1:]))
    p = need(a.name, sys.argv[1:])
    from .project import HOME_DIR
    src = HOME_DIR / "lib" / "current"
    if not (src / "runtime").is_dir():
        die("no installed template; run `ttp setup` from the plugin first")
    h = p.harness
    ident = ["-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost"]
    if _git(h, "status", "--porcelain"):
        _git(h, "add", "-A")
        _git(h, *ident, "commit", "-q", "-m", "local harness changes before template upgrade")
    tmp = p.state / "upgrade-wt"
    if tmp.exists():
        shutil.rmtree(tmp)
    _git(h, "worktree", "add", "-q", str(tmp), "upstream")
    try:
        for part, dst in (("runtime", "runtime"), ("template/prompts", "prompts"), ("template/bin", "bin")):
            if (tmp / dst).exists():
                shutil.rmtree(tmp / dst)
            _copy_tree(src / part, tmp / dst)
        _git(tmp, "add", "-A")
        if _git(tmp, "status", "--porcelain"):
            ver = re.search(r'__version__ = "([^"]+)"', (src / "runtime" / "ttp" / "__init__.py").read_text()).group(1)
            _git(tmp, *ident, "commit", "-q", "-m", f"tt-project template {ver}")
    finally:
        _git(h, "worktree", "remove", "--force", str(tmp))
    r = subprocess.run(["git", "-C", str(h), *ident, "merge", "--no-edit", "upstream"], capture_output=True, text=True)
    if r.returncode != 0:
        print("merge has conflicts; resolve them in", h, "(keep this project's intent, take upstream fixes)")
        print(r.stdout[-1500:])
        sys.exit(1)
    print("harness up to date with the installed template; restarting the daemon")
    from . import service
    print(service.restart(p))


def cmd_alerts(a) -> None:
    p = need(a.name, sys.argv[1:])
    from .notifier import alerts_since
    rows = alerts_since(p, a.after, a.floor)
    if a.json:
        print(json.dumps(rows))
    else:
        for r in rows:
            print(f"#{r['id']} [{r['severity']}] {r['text']}")


def cmd_notifier(a) -> None:
    from . import notifier
    if a.action == "install":
        print(notifier.install())
    elif a.action == "run":
        sys.exit(notifier.main())
    elif a.action == "test":
        notifier.show("tt-project", "Test notification: desktop alerts work on this machine.")
        print("sent a test notification")


def cmd_daemon(a) -> None:
    from .daemon import Daemon
    sys.exit(Daemon(a.dir).run())


def cmd_doctor(a) -> None:
    p = need(a.name, sys.argv[1:])
    from .providers import all_providers
    print(status_text(p))
    for prov in all_providers():
        if prov.name == "fake":
            continue
        print(f"provider {prov.name}: {'found ' + prov.binary() if prov.available() else 'not installed'}"
              + (f" · account {prov.account()}" if prov.available() and prov.account() else ""))
    sec = load_secrets()
    print(f"jev: {'key saved' if (sec.get('jev') or {}).get('key') else 'no key (rules-only screening)'}"
          f" · enabled in project: {p.config()['jev'].get('enabled')}")
    print(f"slack: {'bot saved' if (sec.get('slack') or {}).get('bot_token') else 'not configured'}"
          f" · enabled in project: {p.config()['notify'].get('slack')}")
    print(web_line(p))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ttp", description="tt-project: long-running, self-driving projects")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new", help="create a project")
    s.add_argument("name")
    s.add_argument("--dir", help="project root (default: git top level of the current directory)")
    s.add_argument("--host", help="machine to run on (default: this one)")
    s.add_argument("--describe", help="the brief, inline")
    s.add_argument("--describe-file", help="the brief, from a file ('-' for stdin)")
    s.add_argument("--provider", choices=["claude", "codex", "cursor", "fake"])
    s.add_argument("--no-service", action="store_true", help="do not install a boot-time service")
    s.add_argument("--no-secrets", action="store_true", help="with --host: do not copy your saved keys there")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("web", help="web app link (for a remote project: tunnel command and link)")
    s.add_argument("name")
    s.add_argument("--tunnel", action="store_true", help="open the ssh tunnel now (ask the user first)")
    s.set_defaults(fn=cmd_web)
    for name, fn, hlp in (("connect", cmd_connect, "attach this chat to a project"),
                          ("status", cmd_status, "one-screen status"),
                          ("logs", cmd_logs, "daemon log tail"), ("doctor", cmd_doctor, "diagnose setup")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("name")
        if name == "connect":
            s.add_argument("--chat")
            s.add_argument("--label")
        if name == "status":
            s.add_argument("--json", action="store_true")
        if name == "logs":
            s.add_argument("--bytes", type=int, default=6000)
        s.set_defaults(fn=fn)

    s = sub.add_parser("say", help="send a message to the coordinator")
    s.add_argument("name")
    s.add_argument("text", help="message, or '-' for stdin")
    s.add_argument("--chat")
    s.set_defaults(fn=cmd_say)

    s = sub.add_parser("listen", help="print messages for a chat as they arrive")
    s.add_argument("name")
    s.add_argument("--chat", required=True)
    s.add_argument("--once", action="store_true", help="exit after the first batch")
    s.add_argument("--timeout", type=float, default=0)
    s.set_defaults(fn=cmd_listen)

    s = sub.add_parser("note", help="(inside a run) append a progress note")
    s.add_argument("text")
    s.set_defaults(fn=cmd_note)

    for name, fn in (("list", cmd_list),):
        sub.add_parser(name).set_defaults(fn=fn)
    s = sub.add_parser("find", help="locate a project by name (registry, then chat logs)")
    s.add_argument("name")
    s.set_defaults(fn=cmd_find)
    s = sub.add_parser("adopt", help="record where a project lives")
    s.add_argument("name")
    s.add_argument("--host")
    s.add_argument("--dir", required=True)
    s.set_defaults(fn=cmd_adopt)

    s = sub.add_parser("task", help="add/list/cancel tasks by hand")
    s.add_argument("name")
    s.add_argument("action", choices=["add", "list", "cancel"])
    s.add_argument("title", nargs="?", default="")
    s.add_argument("--spec")
    s.add_argument("--kind", default="work")
    s.add_argument("--tier", default="standard")
    s.add_argument("--priority", type=int, default=3)
    s.set_defaults(fn=cmd_task)

    s = sub.add_parser("memory", help="add a memory")
    s.add_argument("name")
    s.add_argument("text")
    s.add_argument("--kind", default="fact")
    s.set_defaults(fn=cmd_memory)

    for name in ("pause", "resume"):
        s = sub.add_parser(name)
        s.add_argument("name")
        s.set_defaults(fn=cmd_pause)
    for name in ("start", "stop", "restart"):
        s = sub.add_parser(name, help=f"{name} the project's daemon service")
        s.add_argument("name")
        s.set_defaults(fn=cmd_service)

    s = sub.add_parser("config", help="read or set a config key (dotted)")
    s.add_argument("name")
    s.add_argument("key")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("secret", help="store per-user credentials (read from stdin)")
    s.add_argument("kind", choices=["jev", "slack", "show", "push"])
    s.add_argument("--host", help="with push: the machine to copy your saved keys to")
    s.add_argument("--via", default="typesafe", choices=["typesafe", "openrouter"])
    s.add_argument("--url")
    s.add_argument("--email")
    s.add_argument("--user-id")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_secret)

    s = sub.add_parser("setup", help="install ttp for this user (stable copy + shim on PATH)")
    s.add_argument("--bin-dir", default="~/.local/bin")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("upgrade", help="merge the installed tt-project template into a project's harness")
    s.add_argument("name")
    s.set_defaults(fn=cmd_upgrade)

    s = sub.add_parser("alerts", help="broadcast alerts after a message id")
    s.add_argument("name")
    s.add_argument("--after", type=int, default=0)
    s.add_argument("--floor", default="high")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("notifier", help="desktop notifications on this machine for all your projects")
    s.add_argument("action", choices=["install", "run", "test"])
    s.set_defaults(fn=cmd_notifier)

    s = sub.add_parser("daemon", help=argparse.SUPPRESS)
    s.add_argument("dir")
    s.set_defaults(fn=cmd_daemon)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
